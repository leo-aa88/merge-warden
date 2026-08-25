#!/usr/bin/env python3
"""Tests for hierarchical context chunking, packing, and coverage."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

import merge_warden as mw
import review_pipeline as rp
from context_pipeline import (
    DEFAULT_ARCH_COALESCE_CHARS,
    DEFAULT_MAX_SINGLE_CHUNK_CHARS,
    MAX_FAILURE_NOTES_IN_REVIEW,
    ContextChunk,
    CorpusInputs,
    ReviewCorpus,
    build_coverage,
    build_review_corpus,
    chunk_diff,
    chunk_text,
    failed_complete_diff_placeholder,
    format_char_count,
    format_chunk_for_prompt,
    incomplete_coverage_body,
    incomplete_limit_body,
    mark_chunks_covered,
    pack_chunks,
    reset_uncovered,
    resolve_reviewable_diff,
    split_on_headings,
    split_text_by_lines,
    unified_diff_from_file_patches,
)
from review_pipeline import (
    CANDIDATE_FINDINGS_NOT_POSTED,
    DEFAULT_MAP_CONCURRENCY,
    DEFAULT_VALIDATION_CONCURRENCY,
    MAP_CAPACITY_BACKOFF_SECONDS,
    MAP_CAPACITY_RETRIES,
    MAP_HTTP_TIMEOUT_SECONDS,
    MAP_MISSING_CHUNK_RETRIES,
    MAP_TRANSPORT_RETRIES,
    MAX_MAP_ATTEMPTS,
    MAX_MAP_CHUNKS_PER_CALL,
    MAX_MAP_CONCURRENCY,
    MAX_REDUCE_ROUNDS,
    MAX_VALIDATION_CALLS,
    MAX_VALIDATION_CONCURRENCY,
    MAP_CALL_BUDGET_SECONDS,
    MAP_SOFT_REQUEST_TARGET_CHARS,
    PROVIDER_CIRCUIT_FAILURE_THRESHOLD,
    PROVIDER_CIRCUIT_MIN_INDEPENDENT_REQUESTS,
    ProviderFailureKind,
    ProviderHealth,
    ProviderRequestError,
    REDUCE_GROUP_SIZE,
    REDUCE_RESERVE_SECONDS,
    SYNTHESIS_RESERVE_SECONDS,
    SYNTHESIS_SUFFIX,
    VALIDATION_MISSING_CHUNK_RETRIES,
    VALIDATION_RESERVE_SECONDS,
    VALIDATION_STAGE_TOKEN,
    EvidenceStore,
    Finding,
    PipelineDeadlineExceeded,
    PipelineStats,
    StageDeadlineExceeded,
    apply_reduce_decision,
    canonical_finding_id,
    findings_as_review,
    findings_for_context_need,
    format_map_user_message,
    format_validation_user_message,
    hierarchical_reduce,
    ingest_map_result,
    join_confidence,
    join_severity,
    map_stage_deadline,
    normalize_map_concurrency,
    normalize_validation_concurrency,
    pack_map_batches,
    plan_requests,
    plan_validation_tasks,
    prune_context_needs,
    provider_stage_deadline,
    reduce_stage_deadline,
    run_hierarchical_review,
    run_pre_reduce,
    seed_final_reduce,
    sanitize_failure_note,
    validation_path_sort_key,
    validation_related_findings,
    validation_stage_deadline,
)

FOO_MAP_CHUNK = "diff:src/foo.c:1"


def _fid(model_id: str, chunk_id: str = FOO_MAP_CHUNK) -> str:
    return canonical_finding_id(chunk_id, model_id)


def _by_local_id(store: EvidenceStore, model_id: str) -> Finding:
    suffix = f"/{model_id}"
    matches = [item for item in store.findings.values() if item.id.endswith(suffix)]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one finding for local id {model_id!r}, "
            f"got {[item.id for item in matches]}"
        )
    return matches[0]


def _pr(**overrides) -> dict:
    pr = {
        "number": 223,
        "title": "rewrite stdrot",
        "body": "Implement NativeResult ownership.",
        "url": "https://example.test/pr/223",
        "author": {"login": "dev"},
        "baseRefName": "main",
        "headRefName": "feat",
        "headRefOid": "abc123",
        "labels": [],
        "closingIssuesReferences": [],
    }
    pr.update(overrides)
    return pr


def _chunk_ids_in_prompt(user: str) -> list[str]:
    ids: list[str] = []
    prefix = "## CHUNK id="
    marker = " kind="
    for line in user.splitlines():
        if not line.startswith(prefix):
            continue
        rest = line[len(prefix) :]
        if marker in rest:
            ids.append(rest.split(marker, 1)[0])
        else:
            ids.append(rest.split()[0] if rest else "")
    return ids


def _pipeline_model_response(
    system_prompt: str,
    user_message: str,
    final: dict | None = None,
) -> str:
    if "merge-warden-map" in system_prompt:
        return _map_chunks_json(_chunk_ids_in_prompt(user_message))
    if "merge-warden-reduce" in system_prompt:
        return json.dumps({"keep": [], "reject": [], "merge": []})
    return json.dumps(
        final or {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
    )


def _map_chunks_json(
    chunk_ids: list[str],
    *,
    findings: list[dict] | None = None,
    contracts: list[dict] | None = None,
    needs_context: list | None = None,
) -> str:
    items: list[dict] = []
    for index, chunk_id in enumerate(chunk_ids):
        items.append(
            {
                "chunk_id": chunk_id,
                "findings": findings if index == 0 and findings else [],
                "contracts": contracts if index == 0 and contracts else [],
                "dependencies": [],
                "needs_context": needs_context if index == 0 and needs_context else [],
            }
        )
    return json.dumps({"chunks": items})


def _finding(
    finding_id: str,
    body: str | None = None,
    *,
    severity: str = "MAJOR",
    confidence: str = "LIKELY",
    evidence: list[str] | None = None,
    path: str = "a.c",
    line: int | None = 1,
) -> Finding:
    return Finding(
        id=finding_id,
        severity=severity,
        path=path,
        side="RIGHT",
        line=line,
        body=body or f"defect {finding_id}",
        confidence=confidence,
        evidence=list(evidence or []),
    )


def _reduce_payload_ids(user: str) -> list[str]:
    data = json.loads(user[user.find("{") :])
    return [item["id"] for item in data["findings"]]


def _reduce_payload(user: str) -> dict:
    return json.loads(user[user.find("{") :])


def _validation_requested_paths(user: str) -> list[str]:
    marker = "# Context requests from chunk analyses"
    rest = user.split(marker, 1)[1] if marker in user else user
    end = rest.find("# Candidate findings")
    blob = rest[:end] if end != -1 else rest
    paths: list[str] = []
    for line in blob.splitlines():
        if not line.startswith("- `"):
            continue
        path = line[3:].split("`", 1)[0]
        if path:
            paths.append(path)
    return paths


def _validation_candidate_findings(user: str) -> list[dict]:
    marker = "# Candidate findings that requested this context"
    rest = user.split(marker, 1)[1]
    start = rest.find("[")
    end = rest.find("\n# Additional chunks")
    blob = rest[start:end] if end != -1 else rest[start:]
    data = json.loads(blob)
    if not isinstance(data, list):
        raise AssertionError(f"expected candidate findings list, got {type(data)}")
    return data


TYPE_NAME_BODY = (
    "TYPE_NAME token is accepted in lexer/parser positions that should "
    "only allow identifiers, so the grammar cannot distinguish types."
)


def _type_name_finding(*, path: str = "src/lexer.l") -> dict:
    return {
        "id": "F1",
        "severity": "MAJOR",
        "path": path,
        "side": "RIGHT",
        "line": 1,
        "body": TYPE_NAME_BODY,
        "confidence": "LIKELY",
        "evidence": [],
    }


def _map_payloads_json(
    chunk_ids: list[str],
    payload_for,
) -> str:
    items: list[dict] = []
    for chunk_id in chunk_ids:
        extra = payload_for(chunk_id)
        items.append(
            {
                "chunk_id": chunk_id,
                "findings": list(extra.get("findings") or []),
                "contracts": list(extra.get("contracts") or []),
                "dependencies": list(extra.get("dependencies") or []),
                "needs_context": list(extra.get("needs_context") or []),
            }
        )
    return json.dumps({"chunks": items})


def _merge_equivalent_reduce(user: str) -> str:
    data = _reduce_payload(user)
    by_body: dict[str, list[str]] = {}
    for item in data["findings"]:
        by_body.setdefault(item["body"], []).append(item["id"])
    keep: list[str] = []
    merge: list[dict] = []
    for ids in by_body.values():
        if len(ids) == 1:
            keep.append(ids[0])
        else:
            merge.append({"ids": ids, "canonical": ids[0]})
    return json.dumps({"keep": keep, "reject": [], "merge": merge})


def _inputs(**overrides) -> CorpusInputs:
    values = dict(
        pr=_pr(),
        files=[],
        diff="",
        arch_docs=[],
        issues=[],
        omitted_issue_count=0,
        file_contents={},
        commentable={},
        skipped_paths=set(),
    )
    values.update(overrides)
    return CorpusInputs(**values)


def _synthetic_corpus(
    chunks: list[ContextChunk],
    *,
    index: str = "Changed files:\n- src/foo.c +1 -0\n",
    purpose: str = "Title: test\n",
) -> ReviewCorpus:
    coverage = build_coverage(chunks)
    reset_uncovered(coverage, chunks)
    return ReviewCorpus(
        chunks=chunks,
        coverage=coverage,
        index=index,
        purpose_summary=purpose,
        total_chars=sum(chunk.size for chunk in chunks),
    )


def _chunk(chunk_id: str, source: str, text: str, kind: str = "file") -> ContextChunk:
    return ContextChunk(id=chunk_id, kind=kind, source=source, text=text)


class _ReviewRecorder:
    """Acknowledge every supplied chunk and record dispatched user messages."""

    def __init__(
        self,
        *,
        findings: list[dict] | None = None,
        needs_context: list | None = None,
        synthesis_event: str = "COMMENT",
        synthesis_body: str = "# COMMENT\n\nNo defects.\n",
    ) -> None:
        self.map_messages: list[str] = []
        self.validation_messages: list[str] = []
        self.synthesis_messages: list[str] = []
        self.findings = findings
        self.needs_context = needs_context
        self.synthesis_event = synthesis_event
        self.synthesis_body = synthesis_body
        self._lock = threading.Lock()

    def __call__(self, system: str, user: str) -> str:
        if "merge-warden-map" in system:
            ids = _chunk_ids_in_prompt(user)
            if "Context requests" in user or VALIDATION_STAGE_TOKEN in user:
                with self._lock:
                    self.validation_messages.append(user)
                return _map_chunks_json(ids)
            extras: dict = {}
            with self._lock:
                first = not self.map_messages
                self.map_messages.append(user)
            if first:
                if self.findings:
                    extras["findings"] = self.findings
                if self.needs_context:
                    extras["needs_context"] = self.needs_context
            return _map_chunks_json(ids, **extras)
        if "merge-warden-reduce" in system:
            return json.dumps({"keep": [], "reject": [], "merge": []})
        self.synthesis_messages.append(user)
        return json.dumps(
            {
                "event": self.synthesis_event,
                "body": self.synthesis_body,
                "comments": [],
            }
        )


def _assert_unsynthesized_fallback(test: unittest.TestCase, review: dict) -> None:
    test.assertEqual(review["event"], "COMMENT")
    test.assertEqual(review.get("comments") or [], [])
    test.assertEqual(set(review), {"event", "body", "comments"})
    test.assertNotIn("# APPROVE", review["body"])
    test.assertNotIn("# REQUEST CHANGES", review["body"])
    test.assertIn("No approval decision was produced", review["body"])


class ReviewDeadlinePipelineTests(unittest.TestCase):
    def _run(self, call_model, **kwargs):
        corpus = _synthetic_corpus(
            [_chunk("file:a.c:1", "a.c", "int main(void) { return 0; }\n")]
        )
        defaults = dict(
            corpus=corpus,
            synthesis_prompt="synthesis",
            map_prompt="merge-warden-map",
            reduce_prompt="merge-warden-reduce",
            call_model=call_model,
            commentable_section="(none)\n",
            max_map_request_chars=225_000,
            max_reduce_request_chars=225_000,
            map_overhead_chars=24_000,
        )
        defaults.update(kwargs)
        return run_hierarchical_review(**defaults)

    def _mapped_finding(self, *, body: str = "candidate defect") -> dict:
        return {
            "id": "F1",
            "severity": "MAJOR",
            "path": "a.c",
            "side": "RIGHT",
            "line": 1,
            "body": body,
            "confidence": "LIKELY",
            "evidence": ["line 1"],
        }

    def test_deadline_during_map_stops_without_retry_storm(self) -> None:
        calls = 0

        def call_model(_system: str, _user: str) -> str:
            nonlocal calls
            calls += 1
            raise PipelineDeadlineExceeded("provider cutoff reached during map")

        review, coverage, _store, stats = self._run(call_model)
        self.assertEqual(calls, 1)
        _assert_unsynthesized_fallback(self, review)
        self.assertFalse(coverage.complete)
        self.assertTrue(stats.deadline_exhausted)
        self.assertIn("wall-clock review deadline", review["body"])
        self.assertIn("deadline exhausted", stats.footer())

    def test_deadline_before_synthesis_preserves_mapped_findings(self) -> None:
        def call_model(system: str, user: str) -> str:
            if "merge-warden-map" in system:
                return _map_chunks_json(
                    _chunk_ids_in_prompt(user),
                    findings=[self._mapped_finding()],
                )
            raise PipelineDeadlineExceeded("provider cutoff reached before synthesis")

        review, coverage, store, stats = self._run(call_model)
        self.assertTrue(coverage.complete)
        self.assertTrue(stats.deadline_exhausted)
        _assert_unsynthesized_fallback(self, review)
        self.assertEqual(len(store.kept_findings()), 1)
        self.assertEqual(store.kept_findings()[0].body, "candidate defect")
        self.assertNotIn("candidate defect", review["body"])
        self.assertIn(CANDIDATE_FINDINGS_NOT_POSTED, review["body"])
        self.assertIn("Primary context coverage:", review["body"])

    def test_synthesis_retry_deadline_with_time_remaining_fail_closes(self) -> None:
        """A synthesis RequestDeadlineExceeded with remaining > 0 is COMMENT.

        Map classifies that shape as a split-worthy RuntimeError. Synthesis
        must still take the unsynthesized fallback, not crash the action.
        """
        clock = {"now": 10_000.0}
        provider_deadline = clock["now"] + 840.0

        def call_model(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                return _map_chunks_json(
                    _chunk_ids_in_prompt(user),
                    findings=[self._mapped_finding()],
                )
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            classified = mw.classify_deadline_exception(
                stage,
                provider_deadline,
                rp.provider_stage_deadline(stage, provider_deadline),
                mw.RequestDeadlineExceeded("retry would exceed remaining budget"),
            )
            raise classified

        with mock.patch.object(rp.time, "monotonic", lambda: clock["now"]):
            review, coverage, store, stats = self._run(
                call_model, deadline=provider_deadline
            )
        self.assertTrue(coverage.complete)
        self.assertTrue(stats.deadline_exhausted)
        self.assertEqual(stats.synthesis_calls, 0)
        _assert_unsynthesized_fallback(self, review)
        self.assertTrue(store.kept_findings())
        self.assertNotIn("candidate defect", review["body"])
        self.assertIn(CANDIDATE_FINDINGS_NOT_POSTED, review["body"])

    def test_findings_as_review_never_emits_inline_comments(self) -> None:
        store = EvidenceStore()
        store.findings["F1"] = Finding(
            id="F1",
            severity="MAJOR",
            path="a.c",
            side="RIGHT",
            line=1,
            body="raw mapper candidate",
            confidence="LIKELY",
        )
        store.kept.add("F1")
        review = findings_as_review(
            store, "# COMMENT\n\nNo approval decision was produced.\n"
        )
        _assert_unsynthesized_fallback(self, review)
        self.assertEqual(review["comments"], [])
        self.assertNotIn("raw mapper candidate", review["body"])
        self.assertEqual(store.kept_findings()[0].body, "raw mapper candidate")
        self.assertEqual(store.kept_findings()[0].path, "a.c")
        self.assertEqual(store.kept_findings()[0].line, 1)
        self.assertIn(CANDIDATE_FINDINGS_NOT_POSTED, review["body"])

    def test_synthesis_overflow_fail_closes_without_inline_comments(self) -> None:
        def call_model(system: str, user: str) -> str:
            if "merge-warden-map" in system:
                return _map_chunks_json(
                    _chunk_ids_in_prompt(user),
                    findings=[self._mapped_finding(body="overflow candidate")],
                )
            if "merge-warden-reduce" in system:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            raise AssertionError("synthesis must not run when the payload overflows")

        review, coverage, store, stats = self._run(
            call_model, max_reduce_request_chars=80
        )
        self.assertFalse(coverage.complete)
        self.assertEqual(stats.synthesis_calls, 0)
        _assert_unsynthesized_fallback(self, review)
        self.assertTrue(store.kept_findings())
        self.assertIn("overflow candidate", [item.body for item in store.kept_findings()])
        self.assertNotIn("overflow candidate", review["body"])
        self.assertEqual(review.get("comments") or [], [])

    def test_unparseable_synthesis_json_fail_closes(self) -> None:
        """Prose synthesis after successful map/reduce must COMMENT, not raise.

        Issue #47: the synthesis stage raised RuntimeError when the model
        returned a non-JSON body, crashing the Action instead of fail-closing.
        """

        def call_model(system: str, user: str) -> str:
            if "merge-warden-map" in system:
                return _map_chunks_json(
                    _chunk_ids_in_prompt(user),
                    findings=[self._mapped_finding(body="prose candidate")],
                )
            if "merge-warden-reduce" in system:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return "thanks, I will not return JSON"

        review, coverage, store, stats = self._run(call_model)
        _assert_unsynthesized_fallback(self, review)
        self.assertTrue(coverage.complete)
        self.assertFalse(stats.deadline_exhausted)
        self.assertGreater(stats.synthesis_calls, 0)
        self.assertTrue(
            any("synthesis JSON could not be parsed" in note for note in stats.notes)
        )
        self.assertTrue(store.kept_findings())
        self.assertIn("prose candidate", [item.body for item in store.kept_findings()])
        self.assertNotIn("prose candidate", review["body"])
        self.assertEqual(review.get("comments") or [], [])
        self.assertNotEqual(
            mw.normalize_event(review["event"], review["body"]), "APPROVE"
        )

    def test_fenced_truncated_synthesis_garbage_fail_closes(self) -> None:
        """A truncated fenced blob is not a review decision."""

        def call_model(system: str, user: str) -> str:
            if "merge-warden-map" in system:
                return _map_chunks_json(
                    _chunk_ids_in_prompt(user),
                    findings=[self._mapped_finding(body="truncated candidate")],
                )
            if "merge-warden-reduce" in system:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return (
                '```json\n{"event":"APPROVE","body":"# APPROVE\\nLooks good.",'
                '"comments":[{"path":"a.c","line":1,"body":"ship it"}]\n'
            )

        review, coverage, store, stats = self._run(call_model)
        _assert_unsynthesized_fallback(self, review)
        self.assertTrue(coverage.complete)
        self.assertEqual(review.get("comments") or [], [])
        self.assertNotIn("truncated candidate", review["body"])
        self.assertNotIn("ship it", json.dumps(review))
        self.assertTrue(
            any("synthesis JSON could not be parsed" in note for note in stats.notes)
        )
        self.assertTrue(store.findings)
        self.assertNotEqual(
            mw.normalize_event(review["event"], review["body"]), "APPROVE"
        )


class SplitTests(unittest.TestCase):
    def test_split_text_keeps_line_boundaries_and_the_tail(self) -> None:
        text = "\n".join(f"line-{index:04d} " + ("x" * 40) for index in range(40))
        parts = split_text_by_lines(text, 400)
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(parts), text)
        self.assertIn("line-0039", parts[-1])
        for part in parts:
            self.assertLessEqual(len(part), 400)

    def test_heading_split_keeps_sections(self) -> None:
        text = "# One\nhello\n\n# Two\nworld\n"
        sections = split_on_headings(text)
        self.assertEqual(len(sections), 2)
        self.assertTrue(sections[0][0].startswith("# One"))
        self.assertTrue(sections[1][0].startswith("# Two"))

    def test_chunk_text_counts_source_lines_not_diff_semantics(self) -> None:
        chunks = chunk_text(
            prefix="file",
            kind="file",
            source="notes.md",
            text="- deleted looking\n+ added looking\nkeep\n",
            limit=10_000,
            start_line=1,
        )
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].start_line, 1)
        self.assertEqual(chunks[0].end_line, 3)


class DiffChunkTests(unittest.TestCase):
    def test_diff_is_split_by_file_and_hunk_not_discarded(self) -> None:
        hunk_a = "@@ -1,2 +1,2 @@\n-old-a\n+new-a\n"
        hunk_b = "@@ -10,2 +10,2 @@\n-old-b\n+new-b\n"
        hunk_c = "@@ -1,1 +1,1 @@\n-old-c\n+new-c TAIL_MARKER\n"
        diff = (
            "diff --git a/src/a.c b/src/a.c\n"
            "--- a/src/a.c\n"
            "+++ b/src/a.c\n"
            f"{hunk_a}{hunk_b}"
            "diff --git a/src/c.c b/src/c.c\n"
            "--- a/src/c.c\n"
            "+++ b/src/c.c\n"
            f"{hunk_c}"
        )
        chunks = chunk_diff(diff, limit=80)
        texts = "\n".join(chunk.text for chunk in chunks)
        self.assertIn("new-a", texts)
        self.assertIn("new-b", texts)
        self.assertIn("TAIL_MARKER", texts)
        self.assertTrue(any(chunk.source == "src/c.c" for chunk in chunks))
        self.assertGreaterEqual(len(chunks), 2)

    def test_oversized_hunk_is_split_not_truncated(self) -> None:
        body = "+" + ("Z" * 500) + "\n"
        diff = (
            "diff --git a/src/huge.c b/src/huge.c\n"
            "--- a/src/huge.c\n"
            "+++ b/src/huge.c\n"
            "@@ -1,1 +1,1 @@\n"
            f"{body}"
        )
        chunks = chunk_diff(diff, limit=120)
        joined = "".join(chunk.text for chunk in chunks)
        self.assertIn("Z" * 500, joined.replace("\n", ""))
        self.assertGreater(len(chunks), 1)
        self.assertNotIn("truncated", joined)

    def test_two_hunk_chunk_uses_new_file_range_not_raw_diff_lines(self) -> None:
        diff = (
            "diff --git a/foo.c b/foo.c\n"
            "--- a/foo.c\n"
            "+++ b/foo.c\n"
            "@@ -1,3 +1,3 @@\n"
            " int a;\n"
            "-int b;\n"
            "+int B;\n"
            " int c;\n"
            "@@ -500,3 +500,3 @@\n"
            " int y;\n"
            "-int z;\n"
            "+int Z;\n"
            " int w;\n"
        )
        chunks = chunk_diff(diff, limit=100_000)
        self.assertEqual(len(chunks), 1)
        chunk = chunks[0]
        self.assertEqual(chunk.start_line, 1)
        self.assertEqual(chunk.end_line, 502)
        header = format_chunk_for_prompt(chunk).splitlines()[0]
        self.assertIn("lines=1-502", header)
        self.assertIn("@@ -500,3 +500,3 @@", chunk.text)

    def test_single_hunk_right_span_is_last_new_file_line_not_raw_count(self) -> None:
        diff = (
            "diff --git a/foo.c b/foo.c\n"
            "--- a/foo.c\n"
            "+++ b/foo.c\n"
            "@@ -10,2 +10,2 @@\n"
            " keep\n"
            "-old\n"
            "+new\n"
        )
        chunks = chunk_diff(diff, limit=100_000)
        self.assertEqual(len(chunks), 1)
        chunk = chunks[0]
        raw_end = 10 + chunk.text.count("\n") - 1
        self.assertEqual(chunk.start_line, 10)
        self.assertEqual(chunk.end_line, 11)
        self.assertNotEqual(chunk.end_line, raw_end)

    def test_left_only_deletion_hunk_uses_old_file_lines(self) -> None:
        diff = (
            "diff --git a/foo.c b/foo.c\n"
            "--- a/foo.c\n"
            "+++ b/foo.c\n"
            "@@ -40,2 +10,0 @@\n"
            "-gone1\n"
            "-gone2\n"
        )
        chunks = chunk_diff(diff, limit=100_000)
        self.assertEqual(len(chunks), 1)
        chunk = chunks[0]
        self.assertEqual(chunk.start_line, 40)
        self.assertEqual(chunk.end_line, 41)
        header = format_chunk_for_prompt(chunk).splitlines()[0]
        self.assertIn("lines=40-41", header)
        self.assertNotEqual(chunk.start_line, 10)

    def test_mixed_hunk_prefers_right_span_not_longer_left(self) -> None:
        # Three deletions + one addition + one context: LEFT is 10-13, RIGHT is 10-11.
        # Preferring LEFT would still match tests whose two sides share a span.
        diff = (
            "diff --git a/foo.c b/foo.c\n"
            "--- a/foo.c\n"
            "+++ b/foo.c\n"
            "@@ -10,5 +10,2 @@\n"
            "-gone1\n"
            "-gone2\n"
            "-gone3\n"
            "+new\n"
            " keep\n"
        )
        chunks = chunk_diff(diff, limit=100_000)
        self.assertEqual(len(chunks), 1)
        chunk = chunks[0]
        self.assertEqual(chunk.start_line, 10)
        self.assertEqual(chunk.end_line, 11)
        header = format_chunk_for_prompt(chunk).splitlines()[0]
        self.assertIn("lines=10-11", header)
        self.assertNotIn("lines=10-13", header)

    def test_left_only_then_right_hunk_uses_right_span(self) -> None:
        diff = (
            "diff --git a/foo.c b/foo.c\n"
            "--- a/foo.c\n"
            "+++ b/foo.c\n"
            "@@ -40,2 +10,0 @@\n"
            "-gone1\n"
            "-gone2\n"
            "@@ -500,3 +500,3 @@\n"
            " int y;\n"
            "-int z;\n"
            "+int Z;\n"
            " int w;\n"
        )
        chunks = chunk_diff(diff, limit=100_000)
        self.assertEqual(len(chunks), 1)
        chunk = chunks[0]
        self.assertEqual(chunk.start_line, 500)
        self.assertEqual(chunk.end_line, 502)
        header = format_chunk_for_prompt(chunk).splitlines()[0]
        self.assertIn("lines=500-502", header)
        self.assertNotEqual(chunk.start_line, 40)

    def test_no_newline_marker_does_not_extend_right_span(self) -> None:
        diff = (
            "diff --git a/foo.c b/foo.c\n"
            "--- a/foo.c\n"
            "+++ b/foo.c\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
            "\\ No newline at end of file\n"
        )
        chunks = chunk_diff(diff, limit=100_000)
        self.assertEqual(len(chunks), 1)
        chunk = chunks[0]
        self.assertEqual(chunk.start_line, 1)
        self.assertEqual(chunk.end_line, 1)
        header = format_chunk_for_prompt(chunk).splitlines()[0]
        self.assertIn("lines=1-1", header)
        self.assertNotIn("lines=1-2", header)

    def test_oversized_split_continues_file_line_cursor_not_raw_newlines(self) -> None:
        header = (
            "diff --git a/foo.c b/foo.c\n"
            "--- a/foo.c\n"
            "+++ b/foo.c\n"
            "@@ -500,3 +500,3 @@\n"
        )
        first_body = " int y;\n"
        rest_body = "-int z;\n+int Z;\n int w;\n"
        diff = header + first_body + rest_body
        limit = len(header + first_body)
        chunks = chunk_diff(diff, limit=limit)
        self.assertGreater(len(chunks), 1)
        first = chunks[0]
        later = chunks[1:]
        raw_lines = first.text.count("\n")
        if first.text and not first.text.endswith("\n"):
            raw_lines += 1
        bogus_next_start = 500 + raw_lines
        self.assertTrue(any("-int z;" in chunk.text for chunk in later))
        self.assertEqual(first.start_line, 500)
        self.assertEqual(first.end_line, 500)
        self.assertEqual(later[0].start_line, 501)
        self.assertEqual(later[0].end_line, 502)
        for piece in later:
            self.assertNotEqual(piece.start_line, bogus_next_start)
            self.assertIsNotNone(piece.start_line)
            self.assertGreaterEqual(piece.start_line, 500)
            self.assertLess(piece.start_line, bogus_next_start)
        last = later[-1]
        self.assertEqual(last.end_line, 502)

    def test_oversized_hard_split_line_does_not_advance_file_cursor(self) -> None:
        body = "+" + ("Z" * 500) + "\n"
        diff = (
            "diff --git a/src/huge.c b/src/huge.c\n"
            "--- a/src/huge.c\n"
            "+++ b/src/huge.c\n"
            "@@ -1,1 +1,1 @@\n"
            f"{body}"
        )
        chunks = chunk_diff(diff, limit=120)
        plus_chunks = [chunk for chunk in chunks if "Z" in chunk.text]
        self.assertGreater(len(plus_chunks), 1)
        for piece in plus_chunks[1:]:
            if piece.start_line is not None:
                self.assertEqual(piece.start_line, 1)
                self.assertEqual(piece.end_line, 1)


class PackTests(unittest.TestCase):
    def test_pack_chunks_respects_limit_without_dropping(self) -> None:
        chunks = [
            ContextChunk(id=f"c{i}", kind="diff", source="f", text="x" * 30)
            for i in range(5)
        ]
        batches = pack_chunks(chunks, 70)
        packed = [item for batch in batches for item in batch]
        self.assertEqual([chunk.id for chunk in packed], [chunk.id for chunk in chunks])
        for batch in batches:
            self.assertLessEqual(sum(chunk.size for chunk in batch), 70)

    def test_pack_map_batches_balances_by_size_not_source_order(self) -> None:
        chunks = [
            _chunk("huge", "huge.c", "H" * 60_000),
            _chunk("mid", "mid.c", "M" * 30_000),
            _chunk("a", "a.c", "a" * 5_000),
            _chunk("b", "b.c", "b" * 5_000),
            _chunk("c", "c.c", "c" * 5_000),
            _chunk("d", "d.c", "d" * 5_000),
        ]
        greedy = pack_chunks(chunks, 201_000)
        greedy_sizes = [sum(item.size for item in batch) for batch in greedy]
        self.assertEqual(len(greedy), 1)
        self.assertEqual(greedy_sizes[0], 110_000)
        balanced = pack_map_batches(
            chunks,
            max_chars=201_000,
            max_chunks=MAX_MAP_CHUNKS_PER_CALL,
            soft_target=32_000,
        )
        sizes = [sum(item.size for item in batch) for batch in balanced]
        self.assertGreater(len(balanced), 1)
        self.assertNotIn(110_000, sizes)
        self.assertLessEqual(max(sizes) - min(sizes), 60_000)
        packed_ids = [item.id for batch in balanced for item in batch]
        self.assertEqual(sorted(packed_ids), sorted(chunk.id for chunk in chunks))
        for batch in balanced:
            origins = [chunks.index(item) for item in batch]
            self.assertEqual(origins, sorted(origins))

    def test_pack_map_batches_reduces_pathological_spread(self) -> None:
        chunks = [
            _chunk("p64", "p64.c", "A" * 64_000),
            _chunk("p33", "p33.c", "B" * 33_000),
            _chunk("p5a", "p5a.c", "C" * 5_000),
            _chunk("p5b", "p5b.c", "D" * 5_000),
            _chunk("p4a", "p4a.c", "E" * 4_000),
            _chunk("p4b", "p4b.c", "F" * 4_000),
        ]
        balanced = pack_map_batches(
            chunks,
            max_chars=201_000,
            max_chunks=MAX_MAP_CHUNKS_PER_CALL,
            soft_target=MAP_SOFT_REQUEST_TARGET_CHARS,
        )
        sizes = [sum(item.size for item in batch) for batch in balanced]
        self.assertGreaterEqual(len(balanced), 3)
        self.assertLess(max(sizes), 64_000 + 33_000)
        self.assertTrue(any(size == 64_000 for size in sizes))

    def test_default_map_soft_target_keeps_batches_small(self) -> None:
        chunks = [
            _chunk("p11", "p11.c", "A" * 11_000),
            _chunk("p9", "p9.c", "B" * 9_000),
            _chunk("p8", "p8.c", "C" * 8_000),
            _chunk("p7", "p7.c", "D" * 7_000),
            _chunk("p6", "p6.c", "E" * 6_000),
        ]
        balanced = pack_map_batches(
            chunks,
            max_chars=201_000,
            max_chunks=MAX_MAP_CHUNKS_PER_CALL,
            soft_target=MAP_SOFT_REQUEST_TARGET_CHARS,
        )
        sizes = [sum(item.size for item in batch) for batch in balanced]
        self.assertEqual(MAP_SOFT_REQUEST_TARGET_CHARS, 16_000)
        for size, batch in zip(sizes, balanced):
            if all(item.size <= MAP_SOFT_REQUEST_TARGET_CHARS for item in batch):
                self.assertLessEqual(size, MAP_SOFT_REQUEST_TARGET_CHARS)

    def test_pack_map_batches_is_deterministic(self) -> None:
        chunks = [
            _chunk(f"c{i}", f"f{i}.c", ("x" * ((i * 1_733) % 8_000 + 100)))
            for i in range(12)
        ]
        first = pack_map_batches(
            chunks, max_chars=50_000, max_chunks=8, soft_target=32_000
        )
        second = pack_map_batches(
            chunks, max_chars=50_000, max_chunks=8, soft_target=32_000
        )
        self.assertEqual(
            [[item.id for item in batch] for batch in first],
            [[item.id for item in batch] for batch in second],
        )

    def test_pack_map_batches_hard_limit_still_wins(self) -> None:
        chunks = [
            _chunk("a", "a.c", "a" * 40),
            _chunk("b", "b.c", "b" * 40),
        ]
        batches = pack_map_batches(
            chunks, max_chars=50, max_chunks=8, soft_target=10_000
        )
        for batch in batches:
            self.assertLessEqual(sum(item.size for item in batch), 50)
        self.assertEqual(len(batches), 2)


class CorpusTests(unittest.TestCase):
    def test_huge_diff_tail_is_in_corpus(self) -> None:
        tail = "UNIQUE_DIFF_TAIL_249999"
        diff = ("d" * 20_000) + tail
        corpus = build_review_corpus(
            _inputs(
                diff=diff,
                files=[{"filename": "src/foo.c", "status": "modified", "additions": 1, "deletions": 0}],
                file_contents={"src/foo.c": "int main(void) { return 0; }\n"},
            ),
            max_single_chunk_chars=3_000,
            max_context_chunks=64,
        )
        texts = "\n".join(chunk.text for chunk in corpus.chunks)
        self.assertIn(tail, texts)
        self.assertFalse(corpus.limit_error)
        self.assertGreater(len(corpus.reviewable_chunks), 1)

    def test_file_contents_are_lazy_source_chunks_not_primary_map_chunks(self) -> None:
        lines = [f"line-{index:04d}-KEEP" for index in range(1, 201)]
        content = "\n".join(lines) + "\n"
        corpus = build_review_corpus(
            _inputs(
                files=[{"filename": "src/foo.c", "status": "modified", "additions": 200, "deletions": 0}],
                file_contents={"src/foo.c": content},
                diff="diff --git a/src/foo.c b/src/foo.c\n@@ -1 +1 @@\n-a\n+b\n",
            ),
            max_single_chunk_chars=800,
        )
        primary_texts = "\n".join(chunk.text for chunk in corpus.reviewable_chunks)
        self.assertNotIn("line-0001-KEEP", primary_texts)
        self.assertNotIn("line-0200-KEEP", primary_texts)
        texts = "\n".join(chunk.text for chunk in corpus.source_chunks if chunk.kind == "file")
        self.assertIn("line-0001-KEEP", texts)
        self.assertIn("line-0200-KEEP", texts)
        self.assertNotIn("truncated", texts)

    def test_unavailable_file_content_is_not_primary_context(self) -> None:
        corpus = build_review_corpus(
            _inputs(
                files=[{"filename": "src/foo.c", "status": "modified", "additions": 1, "deletions": 0}],
                file_contents={"src/foo.c": None},
                diff="diff --git a/src/foo.c b/src/foo.c\n@@ -1 +1 @@\n-old\n+new\n",
            )
        )
        self.assertFalse(any(chunk.kind == "file" for chunk in corpus.reviewable_chunks))
        self.assertFalse(corpus.source_chunks)
        self.assertIn("new", "\n".join(chunk.text for chunk in corpus.reviewable_chunks))

    def test_binary_exclusion_is_explicit_and_does_not_block_coverage(self) -> None:
        corpus = build_review_corpus(
            _inputs(
                files=[{"filename": "logo.png", "status": "modified", "additions": 0, "deletions": 0}],
                skipped_paths={"logo.png"},
                diff="diff --git a/logo.png b/logo.png\nBinary files differ\n",
            )
        )
        self.assertTrue(any("logo.png" in item for item in corpus.exclusions))
        self.assertTrue(any(chunk.excluded and chunk.source == "logo.png" for chunk in corpus.chunks))
        self.assertIn("logo.png", corpus.index)
        self.assertIn("Explicitly excluded", corpus.index)

    def test_hard_limit_fails_explicitly(self) -> None:
        corpus = build_review_corpus(
            _inputs(diff="x" * 5000, pr=_pr(body="y" * 5000)),
            max_total_review_chars=1000,
            max_single_chunk_chars=400,
        )
        self.assertTrue(corpus.limit_error)
        self.assertIn("did not perform a complete review", corpus.limit_error)
        self.assertNotIn("APPROVE", incomplete_limit_body(corpus.limit_error))
        self.assertIn("MB", format_char_count(1_500_000))

    def test_failed_complete_diff_without_patches_cannot_approve(self) -> None:
        diff = "(failed to load complete diff: GraphQL: pull request diff is too large)\n"
        corpus = build_review_corpus(
            _inputs(
                diff=diff,
                files=[
                    {
                        "filename": "secret.c",
                        "status": "modified",
                        "additions": 50,
                        "deletions": 2,
                    }
                ],
            )
        )
        reviewable = "\n".join(chunk.text for chunk in corpus.reviewable_chunks)
        self.assertTrue(corpus.limit_error)
        self.assertFalse(corpus.coverage.complete)
        self.assertIn("did not perform a complete review", corpus.limit_error)
        self.assertNotIn("APPROVE", incomplete_limit_body(corpus.limit_error))
        self.assertNotIn("failed to load complete diff", reviewable)
        self.assertFalse(
            any(
                chunk.kind == "diff" and not chunk.excluded
                for chunk in corpus.chunks
            )
        )

    def test_failed_complete_diff_uses_file_patches_in_corpus(self) -> None:
        corpus = build_review_corpus(
            _inputs(
                diff="(failed to load complete diff: GraphQL: pull request diff is too large)\n",
                files=[
                    {
                        "filename": "secret.c",
                        "status": "modified",
                        "additions": 50,
                        "deletions": 2,
                        "patch": "@@ -1,2 +1,50 @@\n-old\n+new secret\n",
                    }
                ],
                commentable={"secret.c": {"RIGHT": {1}, "LEFT": {1}}},
            )
        )
        reviewable = "\n".join(chunk.text for chunk in corpus.reviewable_chunks)
        self.assertFalse(corpus.limit_error)
        self.assertIn("new secret", reviewable)
        self.assertTrue(
            any(
                chunk.kind == "diff" and chunk.source == "secret.c"
                for chunk in corpus.reviewable_chunks
            )
        )
        self.assertFalse(
            any(
                chunk.source == "(unknown path)" and not chunk.excluded
                for chunk in corpus.chunks
            )
        )

    def test_failed_complete_diff_mixed_patches_cannot_approve(self) -> None:
        corpus = build_review_corpus(
            _inputs(
                diff=failed_complete_diff_placeholder(
                    "GraphQL: pull request diff is too large"
                ),
                files=[
                    {
                        "filename": "small.c",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 1,
                        "patch": "@@ -1,1 +1,1 @@\n-old small\n+new small\n",
                    },
                    {
                        "filename": "huge.c",
                        "status": "modified",
                        "additions": 4000,
                        "deletions": 0,
                    },
                ],
            )
        )
        reviewable = "\n".join(chunk.text for chunk in corpus.reviewable_chunks)
        self.assertIn("new small", reviewable)
        self.assertTrue(
            any(
                chunk.kind == "diff" and chunk.source == "small.c"
                for chunk in corpus.reviewable_chunks
            )
        )
        self.assertTrue(corpus.limit_error)
        self.assertFalse(corpus.coverage.complete)
        self.assertIn("did not perform a complete review", corpus.limit_error)
        self.assertIn("huge.c", corpus.limit_error)
        self.assertNotIn("no per-file patch payloads were available", corpus.limit_error)
        self.assertNotIn("APPROVE", corpus.limit_error)
        self.assertNotIn("APPROVE", incomplete_limit_body(corpus.limit_error))
        self.assertNotIn("failed to load complete diff", reviewable)
        self.assertNotIn(corpus.limit_error, reviewable)

    def test_failed_complete_diff_skips_binaries_and_zero_zero_files(self) -> None:
        corpus = build_review_corpus(
            _inputs(
                diff=failed_complete_diff_placeholder(
                    "GraphQL: pull request diff is too large"
                ),
                files=[
                    {
                        "filename": "small.c",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 1,
                        "patch": "@@ -1,1 +1,1 @@\n-old small\n+new small\n",
                    },
                    {
                        "filename": "logo.png",
                        "status": "modified",
                        "additions": 0,
                        "deletions": 0,
                    },
                    {
                        "filename": "renamed.bin",
                        "previous_filename": "old.bin",
                        "status": "renamed",
                        "additions": 0,
                        "deletions": 0,
                    },
                ],
                skipped_paths={"logo.png"},
            )
        )
        reviewable = "\n".join(chunk.text for chunk in corpus.reviewable_chunks)
        self.assertIn("new small", reviewable)
        self.assertFalse(corpus.limit_error)
        self.assertNotIn("logo.png", corpus.limit_error)
        self.assertNotIn("renamed.bin", corpus.limit_error)

    def test_unified_diff_from_file_patches_added_header_uses_dev_null(self) -> None:
        diff = unified_diff_from_file_patches(
            [
                {
                    "filename": "new.c",
                    "status": "added",
                    "additions": 1,
                    "deletions": 0,
                    "patch": "@@ -0,0 +1,1 @@\n+int x;\n",
                }
            ]
        )
        self.assertIn("diff --git a/new.c b/new.c", diff)
        self.assertIn("--- /dev/null", diff)
        self.assertIn("+++ b/new.c", diff)
        self.assertIn("+int x;", diff)

    def test_unified_diff_from_file_patches_removed_header_uses_dev_null(self) -> None:
        diff = unified_diff_from_file_patches(
            [
                {
                    "filename": "old.c",
                    "status": "removed",
                    "additions": 0,
                    "deletions": 1,
                    "patch": "@@ -1,1 +0,0 @@\n-int x;\n",
                }
            ]
        )
        self.assertIn("diff --git a/old.c b/old.c", diff)
        self.assertIn("--- a/old.c", diff)
        self.assertIn("+++ /dev/null", diff)
        self.assertIn("-int x;", diff)

    def test_unified_diff_from_file_patches_renamed_uses_previous_filename(self) -> None:
        diff = unified_diff_from_file_patches(
            [
                {
                    "filename": "new_name.c",
                    "previous_filename": "old_name.c",
                    "status": "renamed",
                    "additions": 1,
                    "deletions": 1,
                    "patch": "@@ -1,1 +1,1 @@\n-old\n+new\n",
                }
            ]
        )
        self.assertIn("diff --git a/old_name.c b/new_name.c", diff)
        self.assertIn("--- a/old_name.c", diff)
        self.assertIn("+++ b/new_name.c", diff)

    def test_resolve_reviewable_diff_materializes_generator(self) -> None:
        files = iter(
            [
                {
                    "filename": "small.c",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 1,
                    "patch": "@@ -1,1 +1,1 @@\n-old\n+new\n",
                },
                {
                    "filename": "huge.c",
                    "status": "modified",
                    "additions": 4000,
                    "deletions": 0,
                },
            ]
        )
        reconstructed, missing = resolve_reviewable_diff(
            failed_complete_diff_placeholder("too large"),
            files,
        )
        self.assertTrue(missing)
        self.assertIn("new", reconstructed)
        self.assertIn("small.c", reconstructed)

    def test_arch_docs_do_not_evict_diff(self) -> None:
        marker = "UNIQUE_DIFF_MARKER"
        corpus = build_review_corpus(
            _inputs(
                arch_docs=[("README.md", "ARCH" * 8_000)],
                diff=marker + "\n" + ("d" * 2000),
                files=[{"filename": "src/parser.c", "status": "modified", "additions": 1, "deletions": 1}],
                file_contents={"src/parser.c": "int x;\n"},
            ),
            max_single_chunk_chars=1_500,
        )
        texts = "\n".join(chunk.text for chunk in corpus.chunks)
        self.assertIn(marker, texts)
        self.assertIn("ARCH", texts)
        self.assertGreater(texts.count("ARCH"), 100)


class PipelineTests(unittest.TestCase):
    def test_map_prompt_forbids_final_review(self) -> None:
        prompt = mw.DEFAULT_PROMPT_MAP.read_text(encoding="utf-8")
        self.assertIn("Do not make a merge decision", prompt)
        self.assertIn("merge-warden-map", prompt)
        self.assertNotIn("# APPROVE", prompt)
        self.assertIn('"finding_ids"', prompt)
        self.assertIn("list that finding's ID in finding_ids", prompt)
        self.assertIn("Finding IDs are local to the chunk", prompt)

    def test_reduce_prompt_documents_evidentiary_join(self) -> None:
        prompt = mw.DEFAULT_PROMPT_REDUCE.read_text(encoding="utf-8")
        self.assertIn("Canonical selection chooses identity", prompt)
        self.assertIn("validation:incomplete:", prompt)
        self.assertIn("before cross-context validation", prompt)
        self.assertIn("needs_context", prompt)
        self.assertIn("Do not reject a finding solely because it still needs", prompt)
        self.assertIn("unresolved finding to CONFIRMED", prompt)

    def test_reduce_keeps_original_finding_bodies(self) -> None:
        store = EvidenceStore()
        store.findings["F1"] = Finding(
            id="F1",
            severity="MINOR",
            path="a.c",
            side="RIGHT",
            line=1,
            body="potential lifetime problem",
            confidence="LIKELY",
        )
        store.findings["F2"] = Finding(
            id="F2",
            severity="MAJOR",
            path="a.c",
            side="RIGHT",
            line=2,
            body="other issue",
            confidence="CONFIRMED",
        )
        raw = json.dumps(
            {
                "keep": ["F1"],
                "reject": [{"id": "F2", "reason": "Contradicted by contract C8"}],
                "merge": [],
            }
        )
        apply_reduce_decision(store, raw, ["F1", "F2"])
        self.assertEqual(store.findings["F1"].body, "potential lifetime problem")
        self.assertIn("F2", store.rejected)
        self.assertEqual(store.kept_findings()[0].body, "potential lifetime problem")

    def test_uncovered_chunks_cannot_approve(self) -> None:
        corpus = build_review_corpus(_inputs(diff="+ok\n", pr=_pr(body="small")))

        def fail_map(system: str, user: str) -> str:
            raise RuntimeError("model unavailable")

        review, coverage, _store, _stats = run_hierarchical_review(
            corpus=corpus,
            synthesis_prompt="synth",
            map_prompt="<!-- merge-warden-map -->",
            reduce_prompt="<!-- merge-warden-reduce -->",
            call_model=fail_map,
            commentable_section="(none)\n",
            max_map_request_chars=50_000,
            max_reduce_request_chars=50_000,
            map_overhead_chars=100,
        )
        self.assertFalse(coverage.complete)
        _assert_unsynthesized_fallback(self, review)
        self.assertIn("could not complete a full review", review["body"])

    def test_limit_error_does_not_call_model(self) -> None:
        corpus = build_review_corpus(
            _inputs(diff="x" * 4000),
            max_total_review_chars=10,
            max_single_chunk_chars=50,
        )
        calls = []

        def boom(system: str, user: str) -> str:
            calls.append(1)
            raise AssertionError("should not be called")

        review, coverage, _store, _stats = run_hierarchical_review(
            corpus=corpus,
            synthesis_prompt="synth",
            map_prompt="map",
            reduce_prompt="reduce",
            call_model=boom,
            commentable_section="",
            max_map_request_chars=10_000,
            max_reduce_request_chars=10_000,
            map_overhead_chars=10,
        )
        self.assertEqual(calls, [])
        self.assertEqual(review["event"], "COMMENT")
        self.assertEqual(review.get("comments") or [], [])
        self.assertIn("did not perform a complete review", review["body"])
        self.assertTrue(coverage.limit_error)

    def test_successful_map_then_synthesis(self) -> None:
        corpus = build_review_corpus(_inputs(diff="@@ -1 +1 @@\n-a\n+b\n"))
        calls: list[str] = []

        def fake(system: str, user: str) -> str:
            calls.append(system[:40])
            if "merge-warden-map" in system:
                return _map_chunks_json(
                    _chunk_ids_in_prompt(user),
                    findings=[
                        {
                            "id": "F1",
                            "severity": "MAJOR",
                            "path": "src/foo.c",
                            "side": "RIGHT",
                            "line": 1,
                            "body": "original evidence body",
                            "confidence": "CONFIRMED",
                            "evidence": [],
                        }
                    ],
                    contracts=[{"id": "C1", "text": "owns_string means free"}],
                )
            if "merge-warden-reduce" in system:
                return json.dumps({"keep": ["F1"], "reject": [], "merge": []})
            self.assertIn("original evidence body", user)
            self.assertIn("Do not invent defects", user)
            return json.dumps(
                {
                    "event": "REQUEST_CHANGES",
                    "body": "# REQUEST CHANGES\n\noriginal evidence body\n",
                    "comments": [
                        {
                            "path": "src/foo.c",
                            "side": "RIGHT",
                            "line": 1,
                            "severity": "MAJOR",
                            "body": "original evidence body",
                        }
                    ],
                }
            )

        review, coverage, store, stats = run_hierarchical_review(
            corpus=corpus,
            synthesis_prompt="synthesis prompt",
            map_prompt="<!-- merge-warden-map -->\nmap",
            reduce_prompt="<!-- merge-warden-reduce -->\nreduce",
            call_model=fake,
            commentable_section="(none)\n",
            max_map_request_chars=80_000,
            max_reduce_request_chars=80_000,
            map_overhead_chars=100,
        )
        self.assertTrue(coverage.complete)
        self.assertEqual(review["event"], "REQUEST_CHANGES")
        self.assertEqual(_by_local_id(store, "F1").body, "original evidence body")
        self.assertGreaterEqual(stats.map_calls, 1)
        self.assertEqual(stats.synthesis_calls, 1)
        self.assertEqual(len(review["comments"]), 1)
        self.assertEqual(review["comments"][0]["path"], "src/foo.c")
        self.assertEqual(review["comments"][0]["body"], "original evidence body")

    def test_needs_context_triggers_validation(self) -> None:
        corpus = build_review_corpus(
            _inputs(
                files=[
                    {
                        "filename": "src/foo.c",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 0,
                    },
                    {
                        "filename": "stdrot_api.h",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 0,
                    },
                ],
                file_contents={
                    "src/foo.c": "free(result.value.string);\n",
                    "stdrot_api.h": "typedef struct { bool owns_string; } NativeResult;\n",
                },
                diff=(
                    "diff --git a/src/foo.c b/src/foo.c\n"
                    "@@ -1 +1 @@\n-a\n+b\n"
                    "diff --git a/stdrot_api.h b/stdrot_api.h\n"
                    "@@ -1 +1 @@\n-a\n+b\n"
                ),
            )
        )
        stages: list[str] = []

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system and "Context requests" not in user:
                stages.append("map")
                return _map_chunks_json(
                    _chunk_ids_in_prompt(user),
                    findings=[
                        {
                            "id": "F17",
                            "severity": "BLOCKING",
                            "path": "src/foo.c",
                            "side": "RIGHT",
                            "line": 1,
                            "body": "free of NativeResult string",
                            "confidence": "LIKELY",
                            "evidence": [],
                        }
                    ],
                    needs_context=[
                        {
                            "path": "stdrot_api.h",
                            "reason": "Need ownership contract for NativeResult",
                            "finding_ids": ["F17"],
                        }
                    ],
                )
            if "Context requests" in user:
                stages.append("validation")
                self.assertIn("stdrot_api.h", user)
                self.assertIn("NativeResult", user)
                return _map_chunks_json(
                    _chunk_ids_in_prompt(user),
                    findings=[
                        {
                            "id": "F18",
                            "severity": "BLOCKING",
                            "path": "src/foo.c",
                            "line": 1,
                            "body": "owns_string is ignored before free",
                            "confidence": "CONFIRMED",
                        }
                    ],
                    contracts=[
                        {"id": "C12", "text": "NativeResult owns strings when owns_string=true"}
                    ],
                )
            if "merge-warden-reduce" in system:
                stages.append("reduce")
                return json.dumps(
                    {"keep": ["F17", "F18"], "reject": [], "merge": [{"ids": ["F17", "F18"], "canonical": "F18"}]}
                )
            stages.append("synthesis")
            return json.dumps(
                {"event": "REQUEST_CHANGES", "body": "# REQUEST CHANGES\n", "comments": []}
            )

        review, coverage, store, stats = run_hierarchical_review(
            corpus=corpus,
            synthesis_prompt="synth",
            map_prompt="<!-- merge-warden-map -->",
            reduce_prompt="<!-- merge-warden-reduce -->",
            call_model=fake,
            commentable_section="(none)\n",
            max_map_request_chars=80_000,
            max_reduce_request_chars=80_000,
            map_overhead_chars=100,
        )
        self.assertTrue(coverage.complete)
        self.assertIn("validation", stages)
        self.assertEqual(
            _by_local_id(store, "F18").body, "owns_string is ignored before free"
        )
        self.assertGreaterEqual(stats.validation_calls, 1)
        self.assertEqual(review["event"], "REQUEST_CHANGES")

    def test_needs_context_lazy_loads_requested_file(self) -> None:
        corpus = build_review_corpus(
            _inputs(
                files=[
                    {"filename": "src/foo.c", "status": "modified", "additions": 1, "deletions": 0},
                ],
                file_contents={},
                diff=(
                    "diff --git a/src/foo.c b/src/foo.c\n"
                    "@@ -1 +1 @@\n"
                    "-old\n"
                    "+return borrowed_value();\n"
                ),
            )
        )
        self.assertFalse(corpus.source_chunks)
        loaded: list[str] = []

        def load(path: str) -> str | None:
            loaded.append(path)
            if path == "include/foo.h":
                return "const char *borrowed_value(void);\n"
            return None

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system and "Context requests" not in user:
                self.assertNotIn("borrowed_value(void)", user)
                return _map_chunks_json(
                    _chunk_ids_in_prompt(user),
                    findings=[_likely_foo_finding()],
                    needs_context=[
                        {
                            "path": "include/foo.h",
                            "reason": "Need return ownership contract",
                            "finding_ids": ["F17"],
                        }
                    ],
                )
            if "Context requests" in user:
                self.assertIn("borrowed_value(void)", user)
                return _map_chunks_json(_chunk_ids_in_prompt(user))
            if "merge-warden-reduce" in system:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps({"event": "COMMENT", "body": "# COMMENT\n", "comments": []})

        _review, coverage, store, stats = run_hierarchical_review(
            corpus=corpus,
            synthesis_prompt="synth",
            map_prompt="<!-- merge-warden-map -->",
            reduce_prompt="<!-- merge-warden-reduce -->",
            call_model=fake,
            commentable_section="(none)\n",
            max_map_request_chars=80_000,
            max_reduce_request_chars=80_000,
            map_overhead_chars=100,
            context_loader=load,
        )
        self.assertTrue(coverage.complete)
        self.assertEqual(loaded, ["include/foo.h"])
        self.assertTrue(any(chunk.source == "include/foo.h" for chunk in corpus.source_chunks))
        self.assertNotIn(INCOMPLETE_FOO, _by_local_id(store, "F17").evidence)
        self.assertGreater(stats.validation_request_chars, 0)

    def test_unavailable_requested_context_blocks_approval(self) -> None:
        corpus = build_review_corpus(
            _inputs(
                files=[{"filename": "src/foo.c", "status": "modified", "additions": 1, "deletions": 0}],
                diff=(
                    "diff --git a/src/foo.c b/src/foo.c\n"
                    "@@ -1 +1 @@\n"
                    "-old\n"
                    "+return borrowed_value();\n"
                ),
            )
        )

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system:
                finding = _likely_foo_finding()
                finding["confidence"] = "CONFIRMED"
                return _map_chunks_json(
                    _chunk_ids_in_prompt(user),
                    findings=[finding],
                    needs_context=[
                        {
                            "path": "include/missing.h",
                            "reason": "Need return ownership contract",
                            "finding_ids": ["F17"],
                        }
                    ],
                )
            if "merge-warden-reduce" in system:
                return json.dumps({"keep": [_fid("F17")], "reject": [], "merge": []})
            return json.dumps({"event": "APPROVE", "body": "# APPROVE\n", "comments": []})

        loaded: list[str] = []

        review, coverage, store, stats = run_hierarchical_review(
            corpus=corpus,
            synthesis_prompt="synth",
            map_prompt="<!-- merge-warden-map -->",
            reduce_prompt="<!-- merge-warden-reduce -->",
            call_model=fake,
            commentable_section="(none)\n",
            max_map_request_chars=80_000,
            max_reduce_request_chars=80_000,
            map_overhead_chars=100,
            context_loader=lambda path: loaded.append(path) or None,
        )
        self.assertTrue(coverage.complete)
        self.assertEqual(review["event"], "COMMENT")
        self.assertEqual(loaded, ["include/missing.h"])
        self.assertIn("validation:incomplete:include/missing.h", _by_local_id(store, "F17").evidence)
        self.assertTrue(any("could not load requested context" in note for note in stats.notes))

    def test_unavailable_alias_path_records_canonical_incomplete_marker(self) -> None:
        corpus = build_review_corpus(
            _inputs(
                files=[{"filename": "src/foo.c", "status": "modified", "additions": 1, "deletions": 0}],
                diff=(
                    "diff --git a/src/foo.c b/src/foo.c\n"
                    "@@ -1 +1 @@\n"
                    "-old\n"
                    "+return borrowed_value();\n"
                ),
            )
        )

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system:
                finding = _likely_foo_finding()
                finding["confidence"] = "CONFIRMED"
                return _map_chunks_json(
                    _chunk_ids_in_prompt(user),
                    findings=[finding],
                    needs_context=[
                        {
                            "path": "./include/missing.h",
                            "reason": "Need return ownership contract",
                            "finding_ids": ["F17"],
                        }
                    ],
                )
            if "merge-warden-reduce" in system:
                return json.dumps({"keep": [_fid("F17")], "reject": [], "merge": []})
            return json.dumps({"event": "APPROVE", "body": "# APPROVE\n", "comments": []})

        loaded: list[str] = []

        review, coverage, store, stats = run_hierarchical_review(
            corpus=corpus,
            synthesis_prompt="synth",
            map_prompt="<!-- merge-warden-map -->",
            reduce_prompt="<!-- merge-warden-reduce -->",
            call_model=fake,
            commentable_section="(none)\n",
            max_map_request_chars=80_000,
            max_reduce_request_chars=80_000,
            map_overhead_chars=100,
            context_loader=lambda path: loaded.append(path) or None,
        )
        self.assertTrue(coverage.complete)
        self.assertEqual(review["event"], "COMMENT")
        self.assertEqual(loaded, ["include/missing.h"])
        finding = _by_local_id(store, "F17")
        self.assertIn("validation:incomplete:include/missing.h", finding.evidence)
        self.assertNotIn("validation:incomplete:./include/missing.h", finding.evidence)
        self.assertEqual(set(store.incomplete_context), {"include/missing.h"})
        self.assertTrue(any("could not load requested context" in note for note in stats.notes))

    def test_lazy_source_cache_uses_exact_requested_path(self) -> None:
        map_chunk = _chunk(
            "diff:src/foo.c:1",
            "src/foo.c",
            "+return borrowed_value();\n",
            kind="diff",
        )
        corpus = _synthetic_corpus(
            [map_chunk],
            index="Changed files:\n- src/foo.c +1 -0\n",
        )
        corpus.source_chunks = [
            _chunk("file:vendor/lib/foo.h:1", "vendor/lib/foo.h", "int vendor_only;\n")
        ]
        loaded: list[str] = []

        def load(path: str) -> str | None:
            loaded.append(path)
            if path == "foo.h":
                return "int requested_header;\n"
            return None

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system and "Context requests" not in user:
                return _map_chunks_json(
                    _chunk_ids_in_prompt(user),
                    findings=[_likely_foo_finding()],
                    needs_context=[
                        {
                            "path": "foo.h",
                            "reason": "Need exact requested header",
                            "finding_ids": ["F17"],
                        }
                    ],
                )
            if "Context requests" in user:
                self.assertIn("requested_header", user)
                self.assertNotIn("vendor_only", user)
                return _map_chunks_json(_chunk_ids_in_prompt(user))
            if "merge-warden-reduce" in system:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps({"event": "COMMENT", "body": "# COMMENT\n", "comments": []})

        _review, coverage, store, stats = run_hierarchical_review(
            corpus=corpus,
            synthesis_prompt="synth",
            map_prompt="<!-- merge-warden-map -->",
            reduce_prompt="<!-- merge-warden-reduce -->",
            call_model=fake,
            commentable_section="(none)\n",
            max_map_request_chars=80_000,
            max_reduce_request_chars=80_000,
            map_overhead_chars=100,
            context_loader=load,
        )
        self.assertTrue(coverage.complete)
        self.assertEqual(loaded, ["foo.h"])
        self.assertNotIn("validation:incomplete:foo.h", _by_local_id(store, "F17").evidence)
        self.assertGreater(stats.validation_request_chars, 0)

    def test_context_path_key_preserves_dot_paths(self) -> None:
        self.assertEqual(rp._context_path_key("`./src/ok.c`"), "src/ok.c")
        self.assertEqual(rp._context_path_key(".gitignore"), ".gitignore")
        self.assertEqual(
            rp._context_path_key("./.github/workflows/ci.yml"),
            ".github/workflows/ci.yml",
        )
        self.assertEqual(rp._context_path_key("../shared/config.yml"), "../shared/config.yml")

    def test_ingest_map_result_namespaces_duplicate_ids(self) -> None:
        store = EvidenceStore()
        batch = [
            ContextChunk(id="diff:a:1", kind="diff", source="a.c", text="+x\n"),
        ]
        raw = json.dumps(
            {
                "chunks": [
                    {
                        "chunk_id": "diff:a:1",
                        "findings": [
                            {"id": "F1", "body": "one", "severity": "MAJOR"},
                            {"id": "F1", "body": "two", "severity": "MINOR"},
                        ],
                    }
                ]
            }
        )
        self.assertEqual(ingest_map_result(store, raw, batch, "M1"), {"diff:a:1"})
        self.assertEqual(len(store.findings), 2)
        self.assertNotIn("F1", store.findings)
        self.assertEqual(store.findings[_fid("F1", "diff:a:1")].body, "one")
        self.assertEqual(store.findings[_fid("F2", "diff:a:1")].body, "two")

    def test_incomplete_coverage_body_lists_chunks(self) -> None:
        corpus = build_review_corpus(_inputs(diff="+x\n"))
        corpus.coverage.uncovered_chunk_ids = ["D1", "D2", "D3"]
        body = incomplete_coverage_body(corpus.coverage)
        self.assertIn("3 context chunk(s)", body)
        self.assertIn("`D1`", body)
        review = findings_as_review(EvidenceStore(), body)
        self.assertEqual(review["event"], "COMMENT")
        self.assertEqual(review.get("comments") or [], [])


class MapAcknowledgementTests(unittest.TestCase):
    def test_partial_map_response_does_not_cover_entire_batch(self) -> None:
        corpus = build_review_corpus(_inputs(diff="+ok\n", pr=_pr(body="small")))
        reviewable = corpus.reviewable_chunks
        self.assertGreaterEqual(len(reviewable), 2)
        first_chunk = reviewable[0]
        second_chunk = reviewable[1]
        store = EvidenceStore()
        raw = _map_chunks_json([first_chunk.id])
        acknowledged = ingest_map_result(store, raw, reviewable, "M1")
        self.assertEqual(acknowledged, {first_chunk.id})

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system:
                # Acknowledge only the first supplied chunk, including on retry.
                return _map_chunks_json([first_chunk.id])
            if "merge-warden-reduce" in system:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            raise AssertionError("synthesis must not run when coverage is incomplete")

        review, coverage, _store, stats = run_hierarchical_review(
            corpus=corpus,
            synthesis_prompt="synth",
            map_prompt="<!-- merge-warden-map -->",
            reduce_prompt="<!-- merge-warden-reduce -->",
            call_model=fake,
            commentable_section="(none)\n",
            max_map_request_chars=80_000,
            max_reduce_request_chars=80_000,
            map_overhead_chars=100,
        )
        self.assertFalse(coverage.complete)
        self.assertIn(second_chunk.member_ids[0], coverage.uncovered_chunk_ids)
        _assert_unsynthesized_fallback(self, review)
        self.assertGreaterEqual(stats.map_calls, 2)

    def test_hallucinated_chunk_ids_do_not_count_toward_coverage(self) -> None:
        batch = [
            ContextChunk(id="chunk-1", kind="diff", source="a.c", text="+x\n"),
            ContextChunk(id="chunk-2", kind="diff", source="b.c", text="+y\n"),
        ]
        store = EvidenceStore()
        raw = json.dumps(
            {
                "chunks": [
                    {
                        "chunk_id": "definitely-not-a-real-chunk",
                        "findings": [{"id": "F1", "body": "invented", "severity": "MAJOR"}],
                    }
                ]
            }
        )
        acknowledged = ingest_map_result(store, raw, batch, "M1")
        self.assertEqual(acknowledged, set())
        self.assertEqual(store.findings, {})

        corpus = build_review_corpus(_inputs(diff="+ok\n", pr=_pr(body="small")))

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system:
                return json.dumps(
                    {
                        "chunks": [
                            {
                                "chunk_id": "definitely-not-a-real-chunk",
                                "findings": [],
                            }
                        ]
                    }
                )
            if "merge-warden-reduce" in system:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            raise AssertionError("synthesis must not run when coverage is incomplete")

        review, coverage, _store, _stats = run_hierarchical_review(
            corpus=corpus,
            synthesis_prompt="synth",
            map_prompt="<!-- merge-warden-map -->",
            reduce_prompt="<!-- merge-warden-reduce -->",
            call_model=fake,
            commentable_section="(none)\n",
            max_map_request_chars=80_000,
            max_reduce_request_chars=80_000,
            map_overhead_chars=100,
        )
        self.assertFalse(coverage.complete)
        self.assertTrue(coverage.uncovered_chunk_ids)
        _assert_unsynthesized_fallback(self, review)

    def test_complete_acknowledgement_still_succeeds(self) -> None:
        corpus = build_review_corpus(_inputs(diff="@@ -1 +1 @@\n-a\n+b\n"))
        self.assertGreaterEqual(len(corpus.reviewable_chunks), 1)

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system:
                ids = _chunk_ids_in_prompt(user)
                self.assertTrue(ids)
                return _map_chunks_json(ids)
            if "merge-warden-reduce" in system:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {
                    "event": "COMMENT",
                    "body": "# COMMENT\n\nNo defects.\n",
                    "comments": [],
                }
            )

        review, coverage, _store, stats = run_hierarchical_review(
            corpus=corpus,
            synthesis_prompt="synth",
            map_prompt="<!-- merge-warden-map -->",
            reduce_prompt="<!-- merge-warden-reduce -->",
            call_model=fake,
            commentable_section="(none)\n",
            max_map_request_chars=80_000,
            max_reduce_request_chars=80_000,
            map_overhead_chars=100,
        )
        self.assertTrue(coverage.complete)
        self.assertEqual(stats.synthesis_calls, 1)
        self.assertEqual(review["event"], "COMMENT")

    def test_coalesced_chunk_acknowledgement_covers_member_ids(self) -> None:
        coalesced = ContextChunk(
            id="diff:foo:coalesced",
            kind="diff",
            source="foo.c",
            text="+a\n+b\n",
            member_ids=["diff:foo:1", "diff:foo:2"],
        )
        store = EvidenceStore()
        raw = _map_chunks_json([coalesced.id])
        acknowledged = ingest_map_result(store, raw, [coalesced], "M1")
        self.assertEqual(acknowledged, {coalesced.id})

        coverage = build_coverage([coalesced])
        reset_uncovered(coverage, [coalesced])
        analyzed = [chunk for chunk in [coalesced] if chunk.id in acknowledged]
        mark_chunks_covered(coverage, analyzed)
        self.assertTrue(coverage.complete)
        self.assertNotIn("diff:foo:1", coverage.uncovered_chunk_ids)
        self.assertNotIn("diff:foo:2", coverage.uncovered_chunk_ids)

    def test_retry_can_cover_omitted_chunks(self) -> None:
        corpus = build_review_corpus(_inputs(diff="+ok\n", pr=_pr(body="small")))
        reviewable = corpus.reviewable_chunks
        self.assertGreaterEqual(len(reviewable), 2)
        first_id = reviewable[0].id
        map_calls = {"n": 0}

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system:
                map_calls["n"] += 1
                ids = _chunk_ids_in_prompt(user)
                if map_calls["n"] == 1:
                    return _map_chunks_json([first_id])
                return _map_chunks_json(ids)
            if "merge-warden-reduce" in system:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        review, coverage, _store, stats = run_hierarchical_review(
            corpus=corpus,
            synthesis_prompt="synth",
            map_prompt="<!-- merge-warden-map -->",
            reduce_prompt="<!-- merge-warden-reduce -->",
            call_model=fake,
            commentable_section="(none)\n",
            max_map_request_chars=80_000,
            max_reduce_request_chars=80_000,
            map_overhead_chars=100,
        )
        self.assertTrue(coverage.complete)
        self.assertGreaterEqual(stats.map_calls, 2)
        self.assertEqual(review["event"], "COMMENT")

    def test_malformed_map_response_covers_nothing(self) -> None:
        batch = [ContextChunk(id="chunk-1", kind="diff", source="a.c", text="+x\n")]
        self.assertIsNone(ingest_map_result(EvidenceStore(), "not json", batch, "M1"))


INCOMPLETE_FOO = "validation:incomplete:include/foo.h"


def _header_validation_corpus(parts: int = 3) -> tuple[ReviewCorpus, list[ContextChunk]]:
    map_chunk = _chunk(
        "diff:src/foo.c:1",
        "src/foo.c",
        "+#include \"include/foo.h\"\n",
        kind="diff",
    )
    matching = [
        _chunk(
            f"file:include/foo.h:{index}",
            "include/foo.h",
            f"/* part {index} */\nint field_{index};\n",
        )
        for index in range(1, parts + 1)
    ]
    corpus = _synthetic_corpus(
        [map_chunk],
        index="Changed files:\n- src/foo.c +1 -0\n- include/foo.h +4 -0\n",
    )
    corpus.source_chunks = matching
    return corpus, matching


def _likely_foo_finding() -> dict:
    return {
        "id": "F17",
        "severity": "MAJOR",
        "path": "src/foo.c",
        "side": "RIGHT",
        "line": 1,
        "body": "The returned pointer may outlive its owner.",
        "confidence": "LIKELY",
        "evidence": [],
    }


def _run_hierarchical(corpus: ReviewCorpus, fake, **kwargs):
    defaults = dict(
        corpus=corpus,
        synthesis_prompt="synth",
        map_prompt="<!-- merge-warden-map -->",
        reduce_prompt="<!-- merge-warden-reduce -->",
        call_model=fake,
        commentable_section="(none)\n",
        max_map_request_chars=80_000,
        max_reduce_request_chars=80_000,
        map_overhead_chars=100,
    )
    defaults.update(kwargs)
    return run_hierarchical_review(**defaults)


class ValidationAcknowledgementTests(unittest.TestCase):
    def _run(self, corpus: ReviewCorpus, fake, **kwargs):
        defaults = dict(
            corpus=corpus,
            synthesis_prompt="synth",
            map_prompt="<!-- merge-warden-map -->",
            reduce_prompt="<!-- merge-warden-reduce -->",
            call_model=fake,
            commentable_section="(none)\n",
            max_map_request_chars=80_000,
            max_reduce_request_chars=80_000,
            map_overhead_chars=100,
        )
        defaults.update(kwargs)
        return run_hierarchical_review(**defaults)

    def _model(
        self,
        *,
        validation: str = "all",
        synthesis_event: str = "COMMENT",
        synthesis_body: str = "# COMMENT\n",
    ):
        """Map/reduce/synthesis stub with a validation acknowledgement policy.

        validation:
            "all"            acknowledge every supplied chunk id
            "first"          acknowledge only the first supplied id each call
            "first-then-rest" first call acks one id; later calls ack the rest
            "malformed"      return non-JSON
            "hallucinate"    acknowledge an id that was not supplied
        """
        validation_messages: list[str] = []
        synthesis_messages: list[str] = []
        state = {"validation_calls": 0}

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system:
                ids = _chunk_ids_in_prompt(user)
                if "Context requests" in user:
                    validation_messages.append(user)
                    state["validation_calls"] += 1
                    if validation == "malformed":
                        return "definitely not JSON"
                    if validation == "hallucinate":
                        return _map_chunks_json(["NOT_REAL"])
                    if validation == "first":
                        return _map_chunks_json(ids[:1])
                    if validation == "first-then-rest":
                        if state["validation_calls"] == 1:
                            return _map_chunks_json(ids[:1])
                        return _map_chunks_json(ids)
                    return _map_chunks_json(ids)
                extras: dict = {}
                if not validation_messages:
                    extras["findings"] = [_likely_foo_finding()]
                    extras["needs_context"] = [
                        {
                            "path": "include/foo.h",
                            "reason": "cross-context check",
                            "finding_ids": ["F17"],
                        }
                    ]
                return _map_chunks_json(ids, **extras)
            if "merge-warden-reduce" in system:
                return json.dumps({"keep": ["F17"], "reject": [], "merge": []})
            synthesis_messages.append(user)
            return json.dumps(
                {
                    "event": synthesis_event,
                    "body": synthesis_body,
                    "comments": [],
                }
            )

        fake.validation_messages = validation_messages  # type: ignore[attr-defined]
        fake.synthesis_messages = synthesis_messages  # type: ignore[attr-defined]
        fake.state = state  # type: ignore[attr-defined]
        return fake

    def test_partial_validation_acknowledgement_marks_incomplete(self) -> None:
        corpus, matching = _header_validation_corpus(3)
        fake = self._model(validation="first")
        review, coverage, store, stats = self._run(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertIn(INCOMPLETE_FOO, store.findings[_fid("F17")].evidence)
        self.assertLess(stats.validation_chunks, 3)
        self.assertLess(stats.validation_chunks_acknowledged, len(matching))
        self.assertEqual(review["event"], "COMMENT")

    def test_complete_validation_acknowledgement_succeeds(self) -> None:
        corpus, matching = _header_validation_corpus(3)
        fake = self._model(validation="all")
        review, coverage, store, stats = self._run(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertNotIn(INCOMPLETE_FOO, store.findings[_fid("F17")].evidence)
        self.assertEqual(stats.validation_chunks_acknowledged, len(matching))
        self.assertEqual(review["event"], "COMMENT")

    def test_hallucinated_validation_ids_do_not_count(self) -> None:
        corpus, matching = _header_validation_corpus(2)
        fake = self._model(validation="hallucinate")
        store = EvidenceStore()
        raw = json.dumps(
            {"chunks": [{"chunk_id": "NOT_REAL", "findings": [{"id": "F9", "body": "nope"}]}]}
        )
        acknowledged = ingest_map_result(store, raw, matching, "V1")
        self.assertEqual(acknowledged, set())
        self.assertEqual(store.findings, {})

        _review, coverage, result_store, stats = self._run(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertEqual(stats.validation_chunks_acknowledged, 0)
        self.assertIn(INCOMPLETE_FOO, result_store.findings[_fid("F17")].evidence)

    def test_malformed_validation_json_marks_incomplete(self) -> None:
        corpus, matching = _header_validation_corpus(3)
        fake = self._model(validation="malformed")
        _review, coverage, store, stats = self._run(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertIn(INCOMPLETE_FOO, store.findings[_fid("F17")].evidence)
        self.assertEqual(stats.validation_chunks_acknowledged, 0)
        self.assertEqual(stats.validation_chunks, 0)
        self.assertTrue(any("non-JSON evidence" in note for note in stats.notes))
        self.assertEqual(len(matching), 3)

    def test_retry_recovers_omitted_validation_chunks(self) -> None:
        corpus, matching = _header_validation_corpus(3)
        fake = self._model(validation="first-then-rest")
        _review, coverage, store, stats = self._run(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertNotIn(INCOMPLETE_FOO, store.findings[_fid("F17")].evidence)
        self.assertEqual(stats.validation_chunks_acknowledged, len(matching))
        self.assertEqual(fake.state["validation_calls"], 2)
        self.assertEqual(VALIDATION_MISSING_CHUNK_RETRIES, 1)
        self.assertTrue(any("retrying once" in note for note in stats.notes))
        first_ids = set(_chunk_ids_in_prompt(fake.validation_messages[0]))
        retry_ids = set(_chunk_ids_in_prompt(fake.validation_messages[1]))
        self.assertEqual(first_ids, {chunk.id for chunk in matching})
        self.assertEqual(retry_ids, {chunk.id for chunk in matching[1:]})

    def test_retry_still_failing_leaves_incompleteness(self) -> None:
        corpus, matching = _header_validation_corpus(3)
        fake = self._model(validation="first")
        _review, coverage, store, stats = self._run(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertIn(INCOMPLETE_FOO, store.findings[_fid("F17")].evidence)
        self.assertEqual(stats.validation_chunks_acknowledged, 2)
        self.assertLess(stats.validation_chunks_acknowledged, len(matching))
        self.assertTrue(any("did not acknowledge 1 chunk" in note for note in stats.notes))
        self.assertTrue(any(matching[2].id in note for note in stats.notes))

    def test_validation_call_cap_wins_over_retry(self) -> None:
        corpus, matching = _header_validation_corpus(3)
        fake = self._model(validation="first")
        with mock.patch.object(rp, "MAX_VALIDATION_CALLS", 1):
            _review, coverage, store, stats = self._run(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertEqual(stats.validation_calls, 1)
        self.assertEqual(stats.validation_calls, fake.state["validation_calls"])
        self.assertLess(stats.validation_chunks_acknowledged, len(matching))
        self.assertIn(INCOMPLETE_FOO, store.findings[_fid("F17")].evidence)
        self.assertTrue(any("validation call limit reached" in note for note in stats.notes))

    def test_synthesis_sees_incomplete_validation_marker(self) -> None:
        corpus, _matching = _header_validation_corpus(3)
        fake = self._model(validation="malformed")
        _review, _coverage, store, _stats = self._run(corpus, fake)
        self.assertIn(INCOMPLETE_FOO, store.findings[_fid("F17")].evidence)
        self.assertEqual(store.findings[_fid("F17")].confidence, "LIKELY")
        self.assertTrue(fake.synthesis_messages)
        synthesis_user = fake.synthesis_messages[0]
        self.assertIn(INCOMPLETE_FOO, synthesis_user)
        self.assertIn("validation:incomplete:", SYNTHESIS_SUFFIX)
        self.assertIn("Do not escalate that finding to CONFIRMED", SYNTHESIS_SUFFIX)
        self.assertIn("Do not escalate that finding to CONFIRMED", synthesis_user)
        prompt = mw.DEFAULT_PROMPT.read_text(encoding="utf-8")
        self.assertIn("validation:incomplete:", prompt)
        self.assertIn("unresolved cross-context dependency", prompt)
        self.assertIn("not successfully validated", prompt)

    def test_primary_coverage_unaffected_by_incomplete_validation(self) -> None:
        corpus, matching = _header_validation_corpus(3)
        fake = self._model(validation="hallucinate")
        _review, coverage, store, stats = self._run(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertTrue(stats.coverage_complete)
        self.assertEqual(stats.synthesis_calls, 1)
        self.assertIn(INCOMPLETE_FOO, store.findings[_fid("F17")].evidence)
        self.assertLess(stats.validation_chunks_acknowledged, len(matching))

    def test_exact_approve_with_incomplete_validation_is_comment(self) -> None:
        corpus, _matching = _header_validation_corpus(3)
        fake = self._model(
            validation="first",
            synthesis_event="APPROVE",
            synthesis_body="# APPROVE\n\nLooks good.\n",
        )
        review, coverage, store, _stats = self._run(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertIn(INCOMPLETE_FOO, store.findings[_fid("F17")].evidence)
        self.assertEqual(review["event"], "COMMENT")
        self.assertIn("could not validate all requested context", review["body"])

    def test_exact_approve_with_complete_validation_still_approves(self) -> None:
        corpus, matching = _header_validation_corpus(3)
        fake = self._model(
            validation="all",
            synthesis_event="APPROVE",
            synthesis_body="# APPROVE\n\nLooks good.\n",
        )
        review, coverage, store, stats = self._run(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertNotIn(INCOMPLETE_FOO, store.findings[_fid("F17")].evidence)
        self.assertEqual(stats.validation_chunks_acknowledged, len(matching))
        self.assertEqual(review["event"], "APPROVE")

    def test_alias_approve_events_cannot_pass_incomplete_validation(self) -> None:
        aliases = ("lgtm", "APPROVED", "APPROVE\n", "APPROVE ", "approve.")
        for event in aliases:
            with self.subTest(event=repr(event)):
                corpus, _matching = _header_validation_corpus(3)
                fake = self._model(
                    validation="first",
                    synthesis_event=event,
                    synthesis_body="# APPROVE\n\nLooks good.\n",
                )
                review, coverage, store, _stats = self._run(corpus, fake)
                self.assertTrue(coverage.complete)
                self.assertIn(INCOMPLETE_FOO, store.findings[_fid("F17")].evidence)
                self.assertEqual(review["event"], "COMMENT")
                self.assertNotEqual(
                    mw.normalize_event(review["event"], review["body"]),
                    "APPROVE",
                )
                self.assertIn(
                    "could not validate all requested context",
                    review["body"],
                )


class IncompleteValidationApproveGuardTests(unittest.TestCase):
    """A failed validation stays binding even after the finding is rejected."""

    def _rejected_incomplete_store(self) -> EvidenceStore:
        store = EvidenceStore()
        f1 = Finding(
            "F1", "BLOCKING", "a.c", "RIGHT", 1, "needs foo.h", "LIKELY", ["chunk:c1"],
        )
        f2 = Finding(
            "F2", "MINOR", "b.c", "RIGHT", 2, "style", "CONFIRMED", ["chunk:c2"],
        )
        store.findings["F1"] = f1
        store.findings["F2"] = f2
        rp._mark_incomplete_validation(store, [f1], "foo.h")
        store.kept.update({"F1", "F2"})
        store.reduced = True
        self.assertTrue(rp._has_incomplete_validation(store))
        store.kept.discard("F1")
        store.rejected["F1"] = "unproven after failed validation"
        return store

    def test_rejecting_incomplete_finding_still_blocks_approve(self) -> None:
        store = self._rejected_incomplete_store()
        self.assertEqual(store.incomplete_context, {"foo.h": {"F1"}})
        self.assertTrue(rp._has_incomplete_validation(store))
        event, body = rp.apply_incomplete_validation_guard(
            "APPROVE", "# APPROVE\n", store
        )
        self.assertEqual(event, "COMMENT")
        self.assertIn("could not validate all requested context", body)
        self.assertNotEqual(rp.normalize_event(event, body), "APPROVE")

    def test_unrelated_kept_finding_cannot_approve_after_incomplete_reject(
        self,
    ) -> None:
        store = self._rejected_incomplete_store()
        kept = store.kept_findings()
        self.assertEqual([item.id for item in kept], ["F2"])
        self.assertEqual(kept[0].severity, "MINOR")
        event, body = rp.apply_incomplete_validation_guard(
            "APPROVE", "# APPROVE\n\nLooks good.\n", store
        )
        self.assertEqual(event, "COMMENT")
        self.assertIn("could not validate all requested context", body)

    def test_incomplete_marker_on_rejected_finding_blocks_approve(self) -> None:
        store = EvidenceStore()
        store.findings["F1"] = Finding(
            "F1",
            "BLOCKING",
            "a.c",
            "RIGHT",
            1,
            "needs foo.h",
            "LIKELY",
            ["chunk:c1", "validation:incomplete:foo.h"],
        )
        store.findings["F2"] = Finding(
            "F2", "MINOR", "b.c", "RIGHT", 2, "style", "CONFIRMED", ["chunk:c2"],
        )
        store.kept.add("F2")
        store.rejected["F1"] = "unproven after failed validation"
        store.reduced = True
        self.assertFalse(store.incomplete_context)
        self.assertTrue(rp._has_incomplete_validation(store))
        event, _body = rp.apply_incomplete_validation_guard(
            "APPROVE", "# APPROVE\n", store
        )
        self.assertEqual(event, "COMMENT")

    def test_alias_approve_blocked_after_incomplete_finding_rejected(self) -> None:
        store = self._rejected_incomplete_store()
        event, body = rp.apply_incomplete_validation_guard(
            "lgtm", "# APPROVE\n", store
        )
        self.assertEqual(event, "COMMENT")
        self.assertNotEqual(rp.normalize_event(event, body), "APPROVE")

    def test_complete_validation_without_markers_still_allows_approve(self) -> None:
        store = EvidenceStore()
        store.findings["F2"] = Finding(
            "F2", "MINOR", "b.c", "RIGHT", 2, "style", "CONFIRMED", ["chunk:c2"],
        )
        store.kept.add("F2")
        store.reduced = True
        self.assertFalse(store.incomplete_context)
        self.assertFalse(rp._has_incomplete_validation(store))
        event, body = rp.apply_incomplete_validation_guard(
            "APPROVE", "# APPROVE\n", store
        )
        self.assertEqual(event, "APPROVE")
        self.assertEqual(body, "# APPROVE\n")


class ContextNeedOwnershipTests(unittest.TestCase):
    def _map_then(
        self,
        corpus: ReviewCorpus,
        *,
        findings: list[dict],
        needs_context: list,
        validation,
        synthesis_event: str = "COMMENT",
    ):
        validation_messages: list[str] = []
        synthesis_messages: list[str] = []
        mapped = {"done": False}

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system:
                ids = _chunk_ids_in_prompt(user)
                if "Context requests" in user:
                    validation_messages.append(user)
                    return validation(user, ids)
                extras: dict = {}
                if not mapped["done"]:
                    extras["findings"] = findings
                    extras["needs_context"] = needs_context
                    mapped["done"] = True
                return _map_chunks_json(ids, **extras)
            if "merge-warden-reduce" in system:
                return json.dumps(
                    {
                        "keep": [item["id"] for item in findings],
                        "reject": [],
                        "merge": [],
                    }
                )
            synthesis_messages.append(user)
            return json.dumps(
                {
                    "event": synthesis_event,
                    "body": f"# {synthesis_event}\n",
                    "comments": [],
                }
            )

        fake.validation_messages = validation_messages  # type: ignore[attr-defined]
        fake.synthesis_messages = synthesis_messages  # type: ignore[attr-defined]
        return fake

    def test_ingest_stores_finding_ids(self) -> None:
        store = EvidenceStore()
        batch = [_chunk("diff:src/foo.c:1", "src/foo.c", "+x\n", kind="diff")]
        raw = json.dumps(
            {
                "chunks": [
                    {
                        "chunk_id": "diff:src/foo.c:1",
                        "findings": [_likely_foo_finding()],
                        "needs_context": [
                            {
                                "path": "include/foo.h",
                                "reason": "Need ownership declaration",
                                "finding_ids": ["F17", "F17", ""],
                            }
                        ],
                    }
                ]
            }
        )
        self.assertEqual(ingest_map_result(store, raw, batch, "M1"), {"diff:src/foo.c:1"})
        self.assertEqual(store.needs_context[0].finding_ids, [_fid("F17")])
        self.assertEqual(store.needs_context[0].from_chunk, "diff:src/foo.c:1")

    def test_ingest_drops_unknown_finding_ids(self) -> None:
        store = EvidenceStore()
        batch = [_chunk("diff:src/foo.c:1", "src/foo.c", "+x\n", kind="diff")]
        raw = json.dumps(
            {
                "chunks": [
                    {
                        "chunk_id": "diff:src/foo.c:1",
                        "findings": [_likely_foo_finding()],
                        "needs_context": [
                            {
                                "path": "include/foo.h",
                                "reason": "Need ownership declaration",
                                "finding_ids": ["DOES_NOT_EXIST", "F17"],
                            }
                        ],
                    }
                ]
            }
        )
        ingest_map_result(store, raw, batch, "M1")
        self.assertEqual(store.needs_context[0].finding_ids, [_fid("F17")])

    def test_context_need_marks_finding_without_filename_in_body(self) -> None:
        corpus, _matching = _header_validation_corpus(2)
        fake = self._map_then(
            corpus,
            findings=[_likely_foo_finding()],
            needs_context=[
                {
                    "path": "include/foo.h",
                    "reason": "Need ownership declaration",
                    "finding_ids": ["F17"],
                }
            ],
            validation=lambda _user, _ids: "definitely not JSON",
        )
        _review, coverage, store, _stats = _run_hierarchical(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertIn(INCOMPLETE_FOO, store.findings[_fid("F17")].evidence)
        self.assertNotIn("include/foo.h", store.findings[_fid("F17")].body)

    def test_multiple_findings_may_depend_on_one_context_request(self) -> None:
        corpus, _matching = _header_validation_corpus(2)
        findings = [
            _likely_foo_finding(),
            {
                "id": "F18",
                "severity": "MAJOR",
                "path": "src/foo.c",
                "body": "Caller may free a borrowed buffer.",
                "confidence": "QUESTION",
                "evidence": [],
            },
        ]
        fake = self._map_then(
            corpus,
            findings=findings,
            needs_context=[
                {
                    "path": "include/foo.h",
                    "reason": "Need ownership declaration",
                    "finding_ids": ["F17", "F18"],
                }
            ],
            validation=lambda _user, _ids: "definitely not JSON",
        )
        _review, _coverage, store, _stats = _run_hierarchical(corpus, fake)
        self.assertIn(INCOMPLETE_FOO, store.findings[_fid("F17")].evidence)
        self.assertIn(INCOMPLETE_FOO, store.findings[_fid("F18")].evidence)
        self.assertNotIn("include/foo.h", store.findings[_fid("F17")].body)
        self.assertNotIn("include/foo.h", store.findings[_fid("F18")].body)

    def test_unrelated_finding_is_not_contaminated(self) -> None:
        map_chunk = _chunk(
            "diff:src/foo.c:1",
            "src/foo.c",
            "+use both headers\n",
            kind="diff",
        )
        a_chunk = _chunk("file:a.h:1", "a.h", "int a_contract;\n")
        b_chunk = _chunk("file:b.h:1", "b.h", "int b_contract;\n")
        corpus = _synthetic_corpus(
            [map_chunk],
            index="Changed files:\n- src/foo.c +1 -0\n- a.h +1 -0\n- b.h +1 -0\n",
        )
        corpus.source_chunks = [a_chunk, b_chunk]
        findings = [
            {
                "id": "F1",
                "severity": "MAJOR",
                "path": "src/foo.c",
                "body": "Pointer lifetime is unclear.",
                "confidence": "LIKELY",
                "evidence": [],
            },
            {
                "id": "F2",
                "severity": "MAJOR",
                "path": "src/foo.c",
                "body": "Conversion may overflow; see a.h comments.",
                "confidence": "LIKELY",
                "evidence": [],
            },
        ]

        def validation(user: str, ids: list[str]) -> str:
            header = user.split("# Additional chunks", 1)[0]
            if "`a.h`" in header:
                return "definitely not JSON"
            return _map_chunks_json(ids)

        fake = self._map_then(
            corpus,
            findings=findings,
            needs_context=[
                {
                    "path": "a.h",
                    "reason": "Need a.h contract",
                    "finding_ids": ["F1"],
                },
                {
                    "path": "b.h",
                    "reason": "Need b.h contract",
                    "finding_ids": ["F2"],
                },
            ],
            validation=validation,
        )
        _review, coverage, store, _stats = _run_hierarchical(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertIn("validation:incomplete:a.h", store.findings[_fid("F1")].evidence)
        self.assertNotIn("validation:incomplete:a.h", store.findings[_fid("F2")].evidence)
        self.assertNotIn("validation:incomplete:b.h", store.findings[_fid("F1")].evidence)
        self.assertNotIn("validation:incomplete:b.h", store.findings[_fid("F2")].evidence)

    def test_missing_finding_ids_falls_back_to_originating_chunk(self) -> None:
        corpus, _matching = _header_validation_corpus(2)
        findings = [
            {
                "id": "F1",
                "severity": "MAJOR",
                "path": "src/foo.c",
                "body": "First candidate defect.",
                "confidence": "LIKELY",
                "evidence": [],
            },
            {
                "id": "F2",
                "severity": "MAJOR",
                "path": "src/foo.c",
                "body": "Second candidate defect.",
                "confidence": "QUESTION",
                "evidence": [],
            },
        ]
        fake = self._map_then(
            corpus,
            findings=findings,
            needs_context=[
                {"path": "include/foo.h", "reason": "Need ownership declaration"}
            ],
            validation=lambda _user, _ids: "definitely not JSON",
        )
        _review, _coverage, store, _stats = _run_hierarchical(corpus, fake)
        self.assertEqual(store.needs_context[0].finding_ids, [])
        self.assertIn(INCOMPLETE_FOO, store.findings[_fid("F1")].evidence)
        self.assertIn(INCOMPLETE_FOO, store.findings[_fid("F2")].evidence)

    def test_invalid_finding_id_does_not_crash(self) -> None:
        corpus, _matching = _header_validation_corpus(2)
        fake = self._map_then(
            corpus,
            findings=[_likely_foo_finding()],
            needs_context=[
                {
                    "path": "include/foo.h",
                    "reason": "Need ownership declaration",
                    "finding_ids": ["DOES_NOT_EXIST"],
                }
            ],
            validation=lambda _user, _ids: "definitely not JSON",
        )
        _review, coverage, store, _stats = _run_hierarchical(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertEqual(store.needs_context[0].finding_ids, [])
        self.assertIn(INCOMPLETE_FOO, store.findings[_fid("F17")].evidence)

    def test_successful_validation_produces_no_incomplete_marker(self) -> None:
        corpus, matching = _header_validation_corpus(2)
        fake = self._map_then(
            corpus,
            findings=[_likely_foo_finding()],
            needs_context=[
                {
                    "path": "include/foo.h",
                    "reason": "Need ownership declaration",
                    "finding_ids": ["F17"],
                }
            ],
            validation=lambda _user, ids: _map_chunks_json(ids),
        )
        _review, coverage, store, stats = _run_hierarchical(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertNotIn(INCOMPLETE_FOO, store.findings[_fid("F17")].evidence)
        self.assertNotIn("include/foo.h", store.findings[_fid("F17")].body)
        self.assertEqual(stats.validation_chunks_acknowledged, len(matching))

    def test_findings_for_context_need_prefers_explicit_ids(self) -> None:
        store = EvidenceStore()
        store.findings["F17"] = Finding(
            id="F17",
            severity="MAJOR",
            path="src/foo.c",
            side="RIGHT",
            line=1,
            body="The returned pointer may outlive its owner.",
            confidence="LIKELY",
            evidence=["chunk:diff:src/foo.c:1"],
        )
        store.findings["F18"] = Finding(
            id="F18",
            severity="MAJOR",
            path="src/foo.c",
            side="RIGHT",
            line=2,
            body="Unrelated candidate that mentions include/foo.h in prose.",
            confidence="LIKELY",
            evidence=["chunk:diff:src/foo.c:1"],
        )
        needs = [
            rp.ContextNeed(
                path="include/foo.h",
                reason="Need ownership declaration",
                from_chunk="diff:src/foo.c:1",
                finding_ids=["F17", "DOES_NOT_EXIST"],
            )
        ]
        related = findings_for_context_need(store, needs)
        self.assertEqual([item.id for item in related], ["F17"])

    def test_duplicate_local_finding_ids_resolve_context_need_to_same_chunk_finding(
        self,
    ) -> None:
        store = EvidenceStore()
        chunk_a = _chunk("diff:a.c:1", "a.c", "+bug a\n", kind="diff")
        chunk_b = _chunk("diff:b.c:1", "b.c", "+bug b\n", kind="diff")
        raw_a = json.dumps(
            {
                "chunks": [
                    {
                        "chunk_id": chunk_a.id,
                        "findings": [
                            {"id": "F1", "body": "Bug A", "severity": "MAJOR"}
                        ],
                    }
                ]
            }
        )
        raw_b = json.dumps(
            {
                "chunks": [
                    {
                        "chunk_id": chunk_b.id,
                        "findings": [
                            {"id": "F1", "body": "Bug B", "severity": "MAJOR"}
                        ],
                        "needs_context": [
                            {
                                "path": "b.h",
                                "reason": "Need the declaration for Bug B",
                                "finding_ids": ["F1"],
                            }
                        ],
                    }
                ]
            }
        )
        self.assertEqual(ingest_map_result(store, raw_a, [chunk_a], "M1"), {chunk_a.id})
        self.assertEqual(ingest_map_result(store, raw_b, [chunk_b], "M2"), {chunk_b.id})
        id_a = _fid("F1", chunk_a.id)
        id_b = _fid("F1", chunk_b.id)
        self.assertEqual(set(store.findings), {id_a, id_b})
        self.assertEqual(store.findings[id_a].body, "Bug A")
        self.assertEqual(store.findings[id_b].body, "Bug B")
        self.assertEqual(store.needs_context[0].finding_ids, [id_b])
        related = findings_for_context_need(store, store.needs_context)
        self.assertEqual([item.id for item in related], [id_b])
        self.assertEqual(related[0].body, "Bug B")

    def test_explicit_and_fallback_needs_for_same_path_are_both_resolved(self) -> None:
        store = EvidenceStore()
        store.findings["F1"] = Finding(
            id="F1",
            severity="MAJOR",
            path="a.c",
            side="RIGHT",
            line=1,
            body="Bug from chunk A",
            confidence="LIKELY",
            evidence=["chunk:diff:a.c:1"],
        )
        store.findings["F2"] = Finding(
            id="F2",
            severity="MAJOR",
            path="b.c",
            side="RIGHT",
            line=1,
            body="Bug from chunk B",
            confidence="QUESTION",
            evidence=["chunk:diff:b.c:1"],
        )
        needs = [
            rp.ContextNeed(
                path="common.h",
                reason="Need the shared contract for Bug A",
                from_chunk="diff:a.c:1",
                finding_ids=["F1"],
            ),
            rp.ContextNeed(
                path="common.h",
                reason="Need the shared contract for Bug B",
                from_chunk="diff:b.c:1",
                finding_ids=[],
            ),
        ]
        related = findings_for_context_need(store, needs)
        self.assertEqual({item.id for item in related}, {"F1", "F2"})


class PreReduceBeforeValidationTests(unittest.TestCase):
    """Map findings are triaged before cross-context validation."""

    def _corpus(self, map_chunks: list[ContextChunk], sources: list[ContextChunk]):
        corpus = _synthetic_corpus(
            map_chunks,
            index="Changed files:\n"
            + "\n".join(f"- {chunk.source} +1 -0" for chunk in map_chunks + sources)
            + "\n",
        )
        corpus.source_chunks = sources
        return corpus

    def _pipeline(
        self,
        payload_for,
        *,
        reduce_handler=None,
        on_validation=None,
        synthesis_event: str = "COMMENT",
    ):
        validation_messages: list[str] = []
        reduce_messages: list[str] = []
        synthesis_messages: list[str] = []
        stages: list[str] = []

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system:
                ids = _chunk_ids_in_prompt(user)
                if "Context requests" in user or VALIDATION_STAGE_TOKEN in user:
                    stages.append("validation")
                    validation_messages.append(user)
                    if on_validation is not None:
                        return on_validation(user, ids)
                    return _map_chunks_json(ids)
                stages.append("map")
                return _map_payloads_json(ids, payload_for)
            if "merge-warden-reduce" in system:
                stages.append("reduce")
                reduce_messages.append(user)
                if reduce_handler is not None:
                    return reduce_handler(user)
                return _merge_equivalent_reduce(user)
            stages.append("synthesis")
            synthesis_messages.append(user)
            return json.dumps(
                {
                    "event": synthesis_event,
                    "body": f"# {synthesis_event}\n",
                    "comments": [],
                }
            )

        fake.validation_messages = validation_messages  # type: ignore[attr-defined]
        fake.reduce_messages = reduce_messages  # type: ignore[attr-defined]
        fake.synthesis_messages = synthesis_messages  # type: ignore[attr-defined]
        fake.stages = stages  # type: ignore[attr-defined]
        return fake

    def test_prune_drops_rejected_and_remaps_merged_ids(self) -> None:
        store = EvidenceStore()
        store.findings["A"] = _finding("A", evidence=["chunk:diff:a.c:1"])
        store.findings["B"] = _finding("B", evidence=["chunk:diff:b.c:1"])
        store.findings["C"] = _finding("C", evidence=["chunk:diff:c.c:1"])
        store.kept.add("A")
        store.merged_into["B"] = "A"
        store.rejected["C"] = "unsupported"
        store.needs_context = [
            rp.ContextNeed(
                path="shared.h",
                reason="Need A",
                from_chunk="diff:a.c:1",
                finding_ids=["A"],
            ),
            rp.ContextNeed(
                path="other.h",
                reason="Need B",
                from_chunk="diff:b.c:1",
                finding_ids=["B"],
            ),
            rp.ContextNeed(
                path="noise.h",
                reason="Need C",
                from_chunk="diff:c.c:1",
                finding_ids=["C"],
            ),
        ]
        prune_context_needs(store)
        self.assertEqual(
            [(need.path, need.finding_ids) for need in store.needs_context],
            [("shared.h", ["A"]), ("other.h", ["A"])],
        )
        related = findings_for_context_need(store, store.needs_context)
        self.assertEqual([item.id for item in related], ["A"])

    def test_duplicate_findings_same_context_are_one_validation_target(self) -> None:
        chunks = [
            _chunk(f"diff:src/mod{index}.c:1", f"src/mod{index}.c", "+TYPE_NAME x;\n", kind="diff")
            for index in range(1, 5)
        ]
        header = _chunk("file:src/parser.y:1", "src/parser.y", "%token TYPE_NAME\n")
        corpus = self._corpus(chunks, [header])

        def payload_for(chunk_id: str) -> dict:
            finding = _type_name_finding(path=chunk_id.split(":")[1])
            if chunk_id.endswith("mod1.c:1"):
                finding["severity"] = "BLOCKING"
                finding["confidence"] = "QUESTION"
            return {
                "findings": [finding],
                "needs_context": [
                    {
                        "path": "src/parser.y",
                        "reason": "Need the TYPE_NAME grammar production",
                        "finding_ids": ["F1"],
                    }
                ],
            }

        fake = self._pipeline(payload_for)
        _review, coverage, store, stats = _run_hierarchical(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertEqual(stats.raw_finding_count, 4)
        self.assertEqual(stats.reduced_finding_count, 1)
        self.assertEqual(stats.validation_requests, 1)
        self.assertEqual(len(fake.validation_messages), 1)
        candidates = _validation_candidate_findings(fake.validation_messages[0])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["severity"], "BLOCKING")
        self.assertEqual(candidates[0]["confidence"], "LIKELY")
        self.assertEqual(len(store.kept_findings()), 1)
        self.assertEqual(store.kept_findings()[0].severity, "BLOCKING")
        self.assertEqual(store.kept_findings()[0].confidence, "LIKELY")
        evidence = set(candidates[0]["evidence"])
        for chunk in chunks:
            self.assertIn(f"chunk:{chunk.id}", evidence)
        self.assertIn("4 raw finding(s)", stats.footer())
        self.assertIn("1 after pre-reduce", stats.footer())
        self.assertLess(fake.stages.index("reduce"), fake.stages.index("validation"))

    def test_duplicate_findings_union_distinct_context_requirements(self) -> None:
        chunks = [
            _chunk("diff:src/lexer.l:1", "src/lexer.l", "+TYPE_NAME\n", kind="diff"),
            _chunk("diff:src/parser.y:1", "src/parser.y", "+type_name\n", kind="diff"),
        ]
        sources = [
            _chunk("file:src/lexer.l:1", "src/lexer.l", "TYPE_NAME return IDENT;\n"),
            _chunk("file:src/parser.y:1", "src/parser.y", "%token TYPE_NAME\n"),
        ]
        corpus = self._corpus(chunks, sources)

        def payload_for(chunk_id: str) -> dict:
            path = "src/lexer.l" if "lexer" in chunk_id else "src/parser.y"
            context = "src/parser.y" if path == "src/lexer.l" else "src/lexer.l"
            return {
                "findings": [_type_name_finding(path=path)],
                "needs_context": [
                    {
                        "path": context,
                        "reason": f"Need {context} to confirm TYPE_NAME invariant",
                        "finding_ids": ["F1"],
                    }
                ],
            }

        fake = self._pipeline(payload_for)
        _review, coverage, store, stats = _run_hierarchical(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertEqual(stats.raw_finding_count, 2)
        self.assertEqual(stats.reduced_finding_count, 1)
        self.assertEqual(stats.validation_requests, 2)
        self.assertEqual(len(fake.validation_messages), 2)
        canonical_ids = {
            candidate["id"]
            for message in fake.validation_messages
            for candidate in _validation_candidate_findings(message)
        }
        self.assertEqual(len(canonical_ids), 1)
        self.assertEqual(
            {need.path for need in store.needs_context},
            {"src/lexer.l", "src/parser.y"},
        )
        for need in store.needs_context:
            self.assertEqual(need.finding_ids, [next(iter(canonical_ids))])
        kept = store.kept_findings()
        self.assertEqual(len(kept), 1)
        self.assertGreaterEqual(
            {item for item in kept[0].evidence if item.startswith("chunk:")},
            {f"chunk:{chunk.id}" for chunk in chunks},
        )

    def test_rejected_findings_do_not_trigger_validation(self) -> None:
        chunks = [
            _chunk("diff:src/real.c:1", "src/real.c", "+int real(void);\n", kind="diff"),
            _chunk("diff:src/noise.c:1", "src/noise.c", "+int noise(void);\n", kind="diff"),
        ]
        sources = [
            _chunk("file:include/real.h:1", "include/real.h", "int real(void);\n"),
            _chunk("file:include/noise.h:1", "include/noise.h", "int noise(void);\n"),
        ]
        corpus = self._corpus(chunks, sources)

        def payload_for(chunk_id: str) -> dict:
            if "noise" in chunk_id:
                return {
                    "findings": [
                        {
                            "id": "F1",
                            "severity": "MINOR",
                            "path": "src/noise.c",
                            "body": "speculative noise that is unsupported",
                            "confidence": "QUESTION",
                            "evidence": [],
                        }
                    ],
                    "needs_context": [
                        {
                            "path": "include/noise.h",
                            "reason": "Check the noise header",
                            "finding_ids": ["F1"],
                        }
                    ],
                }
            return {
                "findings": [
                    {
                        "id": "F1",
                        "severity": "MAJOR",
                        "path": "src/real.c",
                        "body": "Ownership of the returned buffer is unclear.",
                        "confidence": "LIKELY",
                        "evidence": [],
                    }
                ],
                "needs_context": [
                    {
                        "path": "include/real.h",
                        "reason": "Need the real ownership contract",
                        "finding_ids": ["F1"],
                    }
                ],
            }

        def reduce_handler(user: str) -> str:
            data = _reduce_payload(user)
            keep: list[str] = []
            reject: list[dict] = []
            for item in data["findings"]:
                if "speculative noise" in item["body"]:
                    reject.append({"id": item["id"], "reason": "unsupported"})
                else:
                    keep.append(item["id"])
            return json.dumps({"keep": keep, "reject": reject, "merge": []})

        fake = self._pipeline(payload_for, reduce_handler=reduce_handler)
        _review, coverage, store, stats = _run_hierarchical(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertEqual(stats.raw_finding_count, 2)
        self.assertEqual(stats.reduced_finding_count, 1)
        self.assertEqual(stats.validation_requests, 1)
        self.assertEqual(len(fake.validation_messages), 1)
        joined = "\n".join(fake.validation_messages)
        self.assertIn("`include/real.h`", joined)
        self.assertNotIn("`include/noise.h`", joined)
        self.assertTrue(any("unsupported" in reason for reason in store.rejected.values()))
        self.assertEqual(len(store.kept_findings()), 1)
        self.assertNotIn("speculative noise", store.kept_findings()[0].body)

    def test_unresolved_merged_finding_is_not_presented_as_confirmed(self) -> None:
        store = EvidenceStore()
        store.findings["A"] = _finding("A", confidence="CONFIRMED", evidence=["chunk:a"])
        store.findings["B"] = _finding("B", confidence="LIKELY", evidence=["chunk:b"])
        store.kept.add("A")
        store.merged_into["B"] = "A"
        store.needs_context = [
            rp.ContextNeed(
                path="src/parser.y",
                reason="Need grammar",
                finding_ids=["A"],
            )
        ]
        views = validation_related_findings(store, [store.findings["A"]])
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0].id, "A")
        self.assertEqual(views[0].confidence, "LIKELY")
        self.assertEqual(store.findings["A"].confidence, "CONFIRMED")
        self.assertEqual(join_confidence(store.merge_members("A")), "CONFIRMED")

    def test_brainrot_shaped_duplicate_type_name_corpus_collapses_before_validation(
        self,
    ) -> None:
        chunks = [
            _chunk(
                f"diff:src/lang_{index}.c:1",
                f"src/lang_{index}.c",
                f"+handle TYPE_NAME in path {index}\n",
                kind="diff",
            )
            for index in range(1, 9)
        ]
        sources = [
            _chunk("file:src/lexer.l:1", "src/lexer.l", "TYPE_NAME {\n  return IDENT;\n}\n"),
            _chunk("file:src/parser.y:1", "src/parser.y", "%token TYPE_NAME\n%%\ntype: TYPE_NAME;\n"),
        ]
        corpus = self._corpus(chunks, sources)

        def payload_for(chunk_id: str) -> dict:
            index = int(chunk_id.rsplit("_", 1)[1].split(".", 1)[0])
            context = "src/lexer.l" if index % 2 else "src/parser.y"
            return {
                "findings": [_type_name_finding(path=chunk_id.split(":")[1])],
                "needs_context": [
                    {
                        "path": context,
                        "reason": "Need the TYPE_NAME lexer/parser invariant",
                        "finding_ids": ["F1"],
                    }
                ],
            }

        fake = self._pipeline(payload_for)
        _review, coverage, store, stats = _run_hierarchical(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertEqual(stats.raw_finding_count, 8)
        self.assertEqual(stats.reduced_finding_count, 1)
        self.assertTrue(fake.validation_messages)
        candidate_counts = [
            len(_validation_candidate_findings(message))
            for message in fake.validation_messages
        ]
        self.assertEqual(set(candidate_counts), {1})
        self.assertEqual(stats.validation_requests, 2)
        canonical_ids = {
            candidate["id"]
            for message in fake.validation_messages
            for candidate in _validation_candidate_findings(message)
        }
        self.assertEqual(len(canonical_ids), 1)
        self.assertIn(f"{stats.raw_finding_count} raw finding(s)", stats.footer())
        self.assertIn("1 after pre-reduce", stats.footer())

    def test_seed_final_reduce_does_not_duplicate_validation_finding(self) -> None:
        store = EvidenceStore()
        run_pre_reduce(
            store,
            "<!-- merge-warden-reduce -->",
            lambda *_args: "",
            50_000,
            PipelineStats(),
        )
        self.assertTrue(store.reduced)
        store.findings["V1"] = _finding("V1", body="from validation")
        self.assertEqual(store.kept_findings(), [])
        seeded = seed_final_reduce(store, mapped_ids=set())
        self.assertEqual([item.id for item in seeded], ["V1"])

        store = EvidenceStore()
        store.findings["A"] = _finding("A")
        store.findings["B"] = _finding("B")
        store.rejected["A"] = "unsupported"
        store.rejected["B"] = "unsupported"
        store.reduced = True
        store.findings["V1"] = _finding("V1", body="from validation")
        self.assertEqual(store.kept_findings(), [])
        seeded = seed_final_reduce(store, mapped_ids={"A", "B"})
        self.assertEqual([item.id for item in seeded], ["V1"])

    def test_validation_finding_is_not_duplicated_when_map_kept_nothing(self) -> None:
        map_chunk = _chunk(
            "diff:src/foo.c:1",
            "src/foo.c",
            "+int main(void);\n",
            kind="diff",
        )
        header = _chunk("file:include/foo.h:1", "include/foo.h", "int ownership;\n")
        corpus = self._corpus([map_chunk], [header])
        reduce_id_lists: list[list[str]] = []

        def payload_for(_chunk_id: str) -> dict:
            return {
                "needs_context": [
                    {"path": "include/foo.h", "reason": "Need the header"}
                ]
            }

        def on_validation(_user: str, ids: list[str]) -> str:
            return _map_chunks_json(
                ids,
                findings=[
                    {
                        "id": "V1",
                        "severity": "MAJOR",
                        "path": "include/foo.h",
                        "body": "ownership is unclear",
                        "confidence": "LIKELY",
                        "evidence": [],
                    }
                ],
            )

        def reduce_handler(user: str) -> str:
            ids = _reduce_payload_ids(user)
            reduce_id_lists.append(ids)
            return json.dumps({"keep": ids, "reject": [], "merge": []})

        fake = self._pipeline(
            payload_for,
            reduce_handler=reduce_handler,
            on_validation=on_validation,
        )
        _review, coverage, store, stats = _run_hierarchical(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertEqual(stats.raw_finding_count, 0)
        self.assertEqual(stats.reduced_finding_count, 0)
        self.assertTrue(fake.validation_messages)
        val_ids = [
            finding_id
            for finding_id in store.findings
            if finding_id.endswith("/V1")
        ]
        self.assertEqual(len(val_ids), 1)
        self.assertEqual([item.id for item in store.kept_findings()], val_ids)
        for ids in reduce_id_lists:
            self.assertEqual(ids, list(dict.fromkeys(ids)))
            self.assertEqual(len(ids), len(set(ids)))


class ValidationAttemptBudgetTests(unittest.TestCase):
    def _paths_corpus(self, count: int) -> tuple[ReviewCorpus, list[str]]:
        paths = [f"include/h{index}.h" for index in range(1, count + 1)]
        map_chunk = _chunk("diff:src/foo.c:1", "src/foo.c", "+int main(void);\n", kind="diff")
        source_chunks: list[ContextChunk] = []
        for path in paths:
            source_chunks.append(_chunk(f"file:{path}:1", path, f"/* {path} */\nint field;\n"))
        corpus = _synthetic_corpus(
            [map_chunk],
            index="Changed files:\n- src/foo.c +1 -0\n",
        )
        corpus.source_chunks = source_chunks
        return corpus, paths

    def _owned_findings(self, paths: list[str]) -> tuple[list[dict], list[dict]]:
        findings = [
            {
                "id": f"F{index}",
                "severity": "MAJOR",
                "path": "src/foo.c",
                "body": f"Candidate {index} may violate an ownership contract.",
                "confidence": "LIKELY",
                "evidence": [],
            }
            for index in range(1, len(paths) + 1)
        ]
        needs = [
            {
                "path": path,
                "reason": "Need the ownership contract",
                "finding_ids": [f"F{index}"],
            }
            for index, path in enumerate(paths, 1)
        ]
        return findings, needs

    def _pipeline_fake(
        self,
        *,
        findings: list[dict],
        needs_context: list,
        on_validation,
    ):
        state = {"map": 0, "validation": 0}
        validation_messages: list[str] = []
        lock = threading.Lock()

        def fake(system: str, user: str) -> str:
            with lock:
                if "merge-warden-map" in system:
                    ids = _chunk_ids_in_prompt(user)
                    if "Context requests" in user:
                        validation_messages.append(user)
                        state["validation"] += 1
                        n = state["validation"]
                        return on_validation(n, user, ids)
                    extras: dict = {}
                    if state["map"] == 0:
                        extras["findings"] = findings
                        extras["needs_context"] = needs_context
                    state["map"] += 1
                    return _map_chunks_json(ids, **extras)
                if "merge-warden-reduce" in system:
                    return json.dumps(
                        {
                            "keep": [item["id"] for item in findings],
                            "reject": [],
                            "merge": [],
                        }
                    )
                return json.dumps(
                    {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
                )

        fake.state = state  # type: ignore[attr-defined]
        fake.validation_messages = validation_messages  # type: ignore[attr-defined]
        return fake

    def test_provider_failures_consume_validation_budget(self) -> None:
        corpus, paths = self._paths_corpus(4)
        findings, needs = self._owned_findings(paths)

        def on_validation(_n: int, _user: str, _ids: list[str]) -> str:
            raise RuntimeError("dead provider")

        fake = self._pipeline_fake(
            findings=findings, needs_context=needs, on_validation=on_validation
        )
        with mock.patch.object(rp, "MAX_VALIDATION_CALLS", 3):
            _review, coverage, store, stats = _run_hierarchical(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertEqual(fake.state["validation"], 3)
        self.assertEqual(stats.validation_attempts, 3)
        self.assertEqual(stats.validation_calls, 3)
        self.assertEqual(stats.validation_calls_succeeded, 0)
        self.assertTrue(
            any("validation call limit reached" in note for note in stats.notes)
        )
        for index, path in enumerate(paths, 1):
            marker = f"validation:incomplete:{path}"
            self.assertIn(marker, store.findings[_fid(f"F{index}")].evidence)

    def test_successful_and_failed_calls_both_consume_budget(self) -> None:
        corpus, paths = self._paths_corpus(4)
        findings, needs = self._owned_findings(paths)

        def on_validation(n: int, _user: str, ids: list[str]) -> str:
            if n == 2:
                raise RuntimeError("dead provider")
            return _map_chunks_json(ids)

        fake = self._pipeline_fake(
            findings=findings, needs_context=needs, on_validation=on_validation
        )
        with mock.patch.object(rp, "MAX_VALIDATION_CALLS", 3):
            _review, coverage, _store, stats = _run_hierarchical(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertEqual(fake.state["validation"], 3)
        self.assertEqual(stats.validation_attempts, 3)
        self.assertEqual(stats.validation_calls, 3)
        self.assertEqual(stats.validation_calls_succeeded, 2)

    def test_missing_chunk_retries_consume_the_same_budget(self) -> None:
        corpus, matching = _header_validation_corpus(3)
        findings = [_likely_foo_finding()]
        needs = [
            {
                "path": "include/foo.h",
                "reason": "Need ownership declaration",
                "finding_ids": ["F17"],
            }
        ]

        def on_validation(_n: int, _user: str, ids: list[str]) -> str:
            return _map_chunks_json(ids[:1])

        fake = self._pipeline_fake(
            findings=findings, needs_context=needs, on_validation=on_validation
        )
        with mock.patch.object(rp, "MAX_VALIDATION_CALLS", 2), mock.patch.object(
            rp, "VALIDATION_MISSING_CHUNK_RETRIES", 5
        ):
            _review, coverage, store, stats = _run_hierarchical(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertEqual(fake.state["validation"], 2)
        self.assertEqual(stats.validation_attempts, 2)
        self.assertEqual(stats.validation_calls, 2)
        self.assertLess(stats.validation_chunks_acknowledged, len(matching))
        self.assertIn(INCOMPLETE_FOO, store.findings[_fid("F17")].evidence)
        self.assertTrue(
            any("validation call limit reached" in note for note in stats.notes)
        )

    def test_http_retries_inside_one_call_count_as_one_attempt(self) -> None:
        corpus, matching = _header_validation_corpus(1)
        findings = [_likely_foo_finding()]
        needs = [
            {
                "path": "include/foo.h",
                "reason": "Need ownership declaration",
                "finding_ids": ["F17"],
            }
        ]
        http = {"n": 0}

        def on_validation(_n: int, _user: str, ids: list[str]) -> str:
            # Transport-level retries stay inside one logical provider call.
            for _ in range(3):
                http["n"] += 1
            return _map_chunks_json(ids)

        fake = self._pipeline_fake(
            findings=findings, needs_context=needs, on_validation=on_validation
        )
        _review, coverage, store, stats = _run_hierarchical(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertEqual(http["n"], 3)
        self.assertEqual(fake.state["validation"], 1)
        self.assertEqual(stats.validation_attempts, 1)
        self.assertEqual(stats.validation_calls_succeeded, 1)
        self.assertEqual(stats.validation_chunks_acknowledged, len(matching))
        self.assertNotIn(INCOMPLETE_FOO, store.findings[_fid("F17")].evidence)


class ValidationStageBudgetTests(unittest.TestCase):
    """Validation must not consume the reserved reduce/synthesis tail."""

    def _paths_corpus(self, count: int) -> tuple[ReviewCorpus, list[str]]:
        paths = [f"include/h{index}.h" for index in range(1, count + 1)]
        map_chunk = _chunk(
            "diff:src/foo.c:1", "src/foo.c", "+int main(void);\n", kind="diff"
        )
        source_chunks: list[ContextChunk] = []
        for path in paths:
            source_chunks.append(
                _chunk(f"file:{path}:1", path, f"/* {path} */\nint field;\n")
            )
        corpus = _synthetic_corpus(
            [map_chunk],
            index="Changed files:\n- src/foo.c +1 -0\n",
        )
        corpus.source_chunks = source_chunks
        return corpus, paths

    def _owned_findings(self, paths: list[str]) -> tuple[list[dict], list[dict]]:
        findings = [
            {
                "id": f"F{index}",
                "severity": "MAJOR",
                "path": "src/foo.c",
                "body": f"Candidate {index} may violate an ownership contract.",
                "confidence": "LIKELY",
                "evidence": [],
            }
            for index in range(1, len(paths) + 1)
        ]
        needs = [
            {
                "path": path,
                "reason": "Need the ownership contract",
                "finding_ids": [f"F{index}"],
            }
            for index, path in enumerate(paths, 1)
        ]
        return findings, needs

    def _pipeline(
        self,
        *,
        findings: list[dict],
        needs_context: list,
        on_validation=None,
        on_reduce=None,
        synthesis_event: str = "COMMENT",
        synthesis_body: str = "# COMMENT\n\nSynthesized review.\n",
    ):
        state = {
            "map": 0,
            "validation": 0,
            "pre-reduce": 0,
            "reduce": 0,
            "synthesis": 0,
        }
        validation_messages: list[str] = []
        synthesis_messages: list[str] = []
        stages: list[str] = []
        lock = threading.Lock()

        def fake(system: str, user: str) -> str:
            with lock:
                stage = mw.provider_call_stage(system, user)
                stages.append(stage)
                state[stage] = state.get(stage, 0) + 1
                if stage == "map":
                    ids = _chunk_ids_in_prompt(user)
                    extras: dict = {}
                    if state["map"] == 1:
                        extras["findings"] = findings
                        extras["needs_context"] = needs_context
                    return _map_chunks_json(ids, **extras)
                if stage == "validation":
                    ids = _chunk_ids_in_prompt(user)
                    validation_messages.append(user)
                    if on_validation is not None:
                        return on_validation(state["validation"], user, ids)
                    return _map_chunks_json(ids)
                if stage in {"pre-reduce", "reduce"}:
                    if on_reduce is not None:
                        return on_reduce(stage, user)
                    return json.dumps({"keep": [], "reject": [], "merge": []})
                synthesis_messages.append(user)
                return json.dumps(
                    {
                        "event": synthesis_event,
                        "body": synthesis_body,
                        "comments": [],
                    }
                )

        fake.state = state  # type: ignore[attr-defined]
        fake.validation_messages = validation_messages  # type: ignore[attr-defined]
        fake.synthesis_messages = synthesis_messages  # type: ignore[attr-defined]
        fake.stages = stages  # type: ignore[attr-defined]
        return fake

    def _invoke_like(
        self,
        *,
        findings: list[dict],
        needs_context: list,
        clock: dict[str, float],
        provider_deadline: float,
        work: dict[str, float],
        synthesis_event: str = "COMMENT",
        synthesis_body: str = "# COMMENT\n\nSynthesized review.\n",
    ):
        """Clamp each stage the way ``merge_warden.invoke`` clamps HTTP."""
        inner = self._pipeline(
            findings=findings,
            needs_context=needs_context,
            synthesis_event=synthesis_event,
            synthesis_body=synthesis_body,
        )
        synthesis_started: list[float] = []
        lock = threading.Lock()

        def fake(system: str, user: str) -> str:
            with lock:
                stage = mw.provider_call_stage(system, user)
                stage_deadline = rp.provider_stage_deadline(stage, provider_deadline)
                remaining = (
                    None
                    if stage_deadline is None
                    else stage_deadline - clock["now"]
                )
                if remaining is not None and remaining <= 0:
                    provider_remaining = provider_deadline - clock["now"]
                    if stage == "map" and provider_remaining > 0:
                        raise StageDeadlineExceeded(
                            "map",
                            f"map stage cutoff reached before {stage}",
                        )
                    raise PipelineDeadlineExceeded(
                        f"provider cutoff reached before {stage}"
                    )
                duration = float(work.get(stage, 0.0))
                if remaining is not None and duration > remaining:
                    clock["now"] = stage_deadline
                    provider_remaining = provider_deadline - clock["now"]
                    if stage == "map" and provider_remaining > 0:
                        raise StageDeadlineExceeded(
                            "map",
                            f"{stage} request crossed the stage deadline",
                        )
                    raise PipelineDeadlineExceeded(
                        f"{stage} request crossed the stage deadline"
                    )
                clock["now"] += duration
                if stage == "synthesis":
                    synthesis_started.append(clock["now"])
                return inner(system, user)

        fake.state = inner.state  # type: ignore[attr-defined]
        fake.validation_messages = inner.validation_messages  # type: ignore[attr-defined]
        fake.synthesis_messages = inner.synthesis_messages  # type: ignore[attr-defined]
        fake.stages = inner.stages  # type: ignore[attr-defined]
        fake.synthesis_started = synthesis_started  # type: ignore[attr-defined]
        return fake

    def test_validation_deadline_subtracts_reduce_and_synthesis_reserves(
        self,
    ) -> None:
        self.assertEqual(REDUCE_RESERVE_SECONDS, 120)
        self.assertEqual(SYNTHESIS_RESERVE_SECONDS, 150)
        self.assertEqual(VALIDATION_RESERVE_SECONDS, 150)
        self.assertEqual(validation_stage_deadline(1840.0), 1570.0)
        self.assertEqual(reduce_stage_deadline(1840.0), 1690.0)
        self.assertEqual(map_stage_deadline(1840.0), 1420.0)
        self.assertIsNone(validation_stage_deadline(None))
        self.assertIsNone(reduce_stage_deadline(None))
        self.assertIsNone(map_stage_deadline(None))
        self.assertEqual(provider_stage_deadline("validation", 1840.0), 1570.0)
        self.assertEqual(provider_stage_deadline("pre-reduce", 1840.0), 1570.0)
        self.assertEqual(provider_stage_deadline("reduce", 1840.0), 1690.0)
        self.assertEqual(provider_stage_deadline("map", 1840.0), 1420.0)
        self.assertEqual(provider_stage_deadline("synthesis", 1840.0), 1840.0)
        self.assertLess(
            provider_stage_deadline("map", 1840.0),
            provider_stage_deadline("validation", 1840.0),
        )
        self.assertIsNone(provider_stage_deadline("validation", None))

    def test_slow_validation_cannot_starve_synthesis(self) -> None:
        """Brainrot PR #238 shape: eight slow validations must not skip synthesis."""
        corpus, paths = self._paths_corpus(8)
        findings, needs = self._owned_findings(paths)
        clock = {"now": 10_000.0}
        provider_budget = 840.0
        provider_deadline = clock["now"] + provider_budget
        fake = self._invoke_like(
            findings=findings,
            needs_context=needs,
            clock=clock,
            provider_deadline=provider_deadline,
            work={
                "map": 0.0,
                "pre-reduce": 0.0,
                "validation": 100.0,
                "reduce": 0.0,
                "synthesis": 10.0,
            },
        )
        with mock.patch.object(rp.time, "monotonic", lambda: clock["now"]):
            review, coverage, store, stats = _run_hierarchical(
                corpus, fake, deadline=provider_deadline
            )
        self.assertTrue(coverage.complete)
        self.assertFalse(stats.deadline_exhausted)
        self.assertTrue(stats.validation_deadline_exhausted)
        self.assertLess(fake.state["validation"], 8)
        self.assertGreater(fake.state["validation"], 0)
        self.assertEqual(fake.state["synthesis"], 1)
        self.assertEqual(stats.synthesis_calls, 1)
        self.assertIn("Synthesized review.", review["body"])
        self.assertIn("validation budget exhausted", stats.footer())
        self.assertLessEqual(
            fake.synthesis_started[0],
            provider_deadline,
        )
        incomplete_markers = [
            f"validation:incomplete:{path}"
            for path in paths
            if f"validation:incomplete:{path}"
            in store.findings[_fid(f"F{paths.index(path) + 1}")].evidence
        ]
        self.assertGreater(len(incomplete_markers), 0)
        self.assertIn("validation", fake.stages)
        self.assertIn("synthesis", fake.stages)
        self.assertLess(fake.stages.index("validation"), fake.stages.index("synthesis"))

    def test_incomplete_validation_from_deadline_reaches_synthesis(self) -> None:
        corpus, paths = self._paths_corpus(8)
        findings, needs = self._owned_findings(paths)
        clock = {"now": 10_000.0}

        def on_validation(_n: int, _user: str, ids: list[str]) -> str:
            clock["now"] += 100.0
            return _map_chunks_json(ids)

        fake = self._pipeline(
            findings=findings,
            needs_context=needs,
            on_validation=on_validation,
            synthesis_event="APPROVE",
            synthesis_body="# APPROVE\n\nLooks good.\n",
        )
        with mock.patch.object(rp.time, "monotonic", lambda: clock["now"]):
            review, coverage, store, stats = _run_hierarchical(
                corpus, fake, deadline=clock["now"] + 840.0
            )
        self.assertTrue(coverage.complete)
        self.assertEqual(stats.synthesis_calls, 1)
        self.assertTrue(fake.synthesis_messages)
        synthesis_user = fake.synthesis_messages[0]
        incomplete_in_store = [
            item
            for finding in store.kept_findings()
            for item in finding.evidence
            if item.startswith("validation:incomplete:")
        ]
        self.assertTrue(incomplete_in_store)
        for marker in incomplete_in_store:
            self.assertIn(marker, synthesis_user)
        self.assertEqual(review["event"], "COMMENT")
        self.assertIn("could not validate all requested context", review["body"])

    def test_validation_deadline_does_not_load_remaining_context(self) -> None:
        corpus, paths = self._paths_corpus(4)
        findings, needs = self._owned_findings(paths)
        corpus.source_chunks = []
        loaded: list[str] = []

        def loader(path: str) -> str | None:
            loaded.append(path)
            return f"/* {path} */\nint field;\n"

        clock = {"now": 10_000.0}
        provider_deadline = clock["now"] + 840.0
        fake = self._invoke_like(
            findings=findings,
            needs_context=needs,
            clock=clock,
            provider_deadline=provider_deadline,
            work={"map": 0.0, "validation": 10_000.0, "synthesis": 0.0},
        )
        with mock.patch.object(rp.time, "monotonic", lambda: clock["now"]):
            _run_hierarchical(
                corpus,
                fake,
                deadline=provider_deadline,
                context_loader=loader,
                validation_concurrency=1,
            )
        self.assertEqual(loaded, [paths[0]])
        self.assertEqual(fake.state["validation"], 0)

    def test_no_validation_call_starts_once_stage_deadline_is_reached(self) -> None:
        """A deadline that leaves only reduce+synthesis also expires map.

        Map's cutoff is 150s before validation's. Starting already inside the
        reduce+synthesis window must not start map or validation; synthesis
        still runs on whatever evidence exists.
        """
        corpus, paths = self._paths_corpus(4)
        findings, needs = self._owned_findings(paths)
        clock = {"now": 10_000.0}
        provider_deadline = (
            clock["now"] + REDUCE_RESERVE_SECONDS + SYNTHESIS_RESERVE_SECONDS
        )

        fake = self._pipeline(findings=findings, needs_context=needs)
        with mock.patch.object(rp.time, "monotonic", lambda: clock["now"]):
            review, coverage, _store, stats = _run_hierarchical(
                corpus, fake, deadline=provider_deadline
            )
        self.assertEqual(fake.state["map"], 0)
        self.assertEqual(fake.state["validation"], 0)
        self.assertEqual(stats.validation_attempts, 0)
        self.assertTrue(stats.map_deadline_exhausted)
        self.assertFalse(stats.deadline_exhausted)
        self.assertEqual(stats.synthesis_calls, 1)
        self.assertIn("Synthesized review.", review["body"])
        self.assertFalse(coverage.complete)
        self.assertNotEqual(review["event"].upper().replace(" ", "_"), "APPROVE")

    def test_validation_deadline_exception_continues_to_synthesis(self) -> None:
        corpus, paths = self._paths_corpus(4)
        findings, needs = self._owned_findings(paths)

        def on_validation(n: int, _user: str, ids: list[str]) -> str:
            if n >= 2:
                raise PipelineDeadlineExceeded(
                    "provider cutoff reached before validation"
                )
            return _map_chunks_json(ids)

        fake = self._pipeline(
            findings=findings, needs_context=needs, on_validation=on_validation
        )
        review, coverage, store, stats = _run_hierarchical(
            corpus, fake, validation_concurrency=1
        )
        self.assertTrue(coverage.complete)
        self.assertFalse(stats.deadline_exhausted)
        self.assertTrue(stats.validation_deadline_exhausted)
        self.assertEqual(fake.state["validation"], 2)
        self.assertEqual(stats.synthesis_calls, 1)
        self.assertIn("Synthesized review.", review["body"])
        self.assertIn(
            "validation:incomplete:include/h2.h",
            store.findings[_fid("F2")].evidence,
        )
        self.assertIn(
            "validation:incomplete:include/h3.h",
            store.findings[_fid("F3")].evidence,
        )
        self.assertIn(
            "validation:incomplete:include/h4.h",
            store.findings[_fid("F4")].evidence,
        )

    def test_slow_reduce_cannot_starve_synthesis(self) -> None:
        """A greedy post-validation reduce must stop before the synthesis floor."""
        corpus, paths = self._paths_corpus(8)
        findings, needs = self._owned_findings(paths)
        clock = {"now": 10_000.0}
        provider_budget = 840.0
        provider_deadline = clock["now"] + provider_budget
        fake = self._invoke_like(
            findings=findings,
            needs_context=needs,
            clock=clock,
            provider_deadline=provider_deadline,
            work={
                "map": 0.0,
                "pre-reduce": 0.0,
                "validation": 100.0,
                "reduce": 140.0,
                "synthesis": 10.0,
            },
        )
        with mock.patch.object(rp.time, "monotonic", lambda: clock["now"]):
            review, coverage, _store, stats = _run_hierarchical(
                corpus, fake, deadline=provider_deadline
            )
        self.assertTrue(coverage.complete)
        self.assertFalse(stats.deadline_exhausted)
        self.assertTrue(stats.validation_deadline_exhausted)
        self.assertTrue(stats.reduce_deadline_exhausted)
        self.assertEqual(stats.synthesis_calls, 1)
        self.assertEqual(fake.state["synthesis"], 1)
        self.assertIn("Synthesized review.", review["body"])
        self.assertIn("reduce budget exhausted", stats.footer())
        synthesis_started = fake.synthesis_started[0]
        self.assertLessEqual(synthesis_started, provider_deadline)
        self.assertGreaterEqual(
            provider_deadline - synthesis_started,
            SYNTHESIS_RESERVE_SECONDS - 10.0,
        )

    def test_pre_reduce_deadline_continues_to_synthesis(self) -> None:
        corpus, paths = self._paths_corpus(8)
        findings, needs = self._owned_findings(paths)
        clock = {"now": 10_000.0}
        provider_deadline = clock["now"] + 840.0
        fake = self._invoke_like(
            findings=findings,
            needs_context=needs,
            clock=clock,
            provider_deadline=provider_deadline,
            work={
                "map": 0.0,
                "pre-reduce": 200.0,
                "validation": 100.0,
                "reduce": 0.0,
                "synthesis": 10.0,
            },
        )
        with mock.patch.object(rp.time, "monotonic", lambda: clock["now"]):
            review, coverage, _store, stats = _run_hierarchical(
                corpus, fake, deadline=provider_deadline
            )
        self.assertTrue(coverage.complete)
        self.assertFalse(stats.deadline_exhausted)
        self.assertTrue(stats.pre_reduce_deadline_exhausted)
        self.assertFalse(stats.reduce_deadline_exhausted)
        self.assertTrue(stats.validation_deadline_exhausted)
        self.assertEqual(stats.synthesis_calls, 1)
        self.assertIn("Synthesized review.", review["body"])
        self.assertIn("pre-reduce budget exhausted", stats.footer())
        synthesis_started = fake.synthesis_started[0]
        self.assertLessEqual(synthesis_started, provider_deadline)
        self.assertGreaterEqual(
            provider_deadline - synthesis_started,
            SYNTHESIS_RESERVE_SECONDS - 10.0,
        )

    def test_real_sleep_validation_cannot_consume_synthesis_reserve(self) -> None:
        corpus, paths = self._paths_corpus(8)
        findings, needs = self._owned_findings(paths)

        def on_validation(_n: int, _user: str, ids: list[str]) -> str:
            time.sleep(0.05)
            return _map_chunks_json(ids)

        fake = self._pipeline(
            findings=findings, needs_context=needs, on_validation=on_validation
        )
        with mock.patch.object(rp, "VALIDATION_RESERVE_SECONDS", 0.2), mock.patch.object(
            rp, "REDUCE_RESERVE_SECONDS", 0.2
        ), mock.patch.object(rp, "SYNTHESIS_RESERVE_SECONDS", 0.2), mock.patch.object(
            rp, "MAP_CALL_BUDGET_SECONDS", 0.0
        ):
            started = time.monotonic()
            review, coverage, _store, stats = _run_hierarchical(
                corpus,
                fake,
                deadline=started + 0.75,
                validation_concurrency=1,
            )
        self.assertTrue(coverage.complete)
        self.assertFalse(stats.deadline_exhausted)
        self.assertTrue(stats.validation_deadline_exhausted)
        self.assertLess(fake.state["validation"], 8)
        self.assertEqual(stats.synthesis_calls, 1)
        self.assertEqual(fake.state["synthesis"], 1)
        self.assertIn("Synthesized review.", review["body"])

    def _locatable_findings(
        self, paths: list[str]
    ) -> tuple[list[dict], list[dict]]:
        findings, needs = self._owned_findings(paths)
        for item in findings:
            item["side"] = "RIGHT"
            item["line"] = 1
        return findings, needs

    def test_validation_deadline_then_synthesis_timeout_posts_no_inline_comments(
        self,
    ) -> None:
        corpus, paths = self._paths_corpus(4)
        findings, needs = self._locatable_findings(paths)

        def on_validation(n: int, _user: str, ids: list[str]) -> str:
            if n >= 2:
                raise PipelineDeadlineExceeded(
                    "provider cutoff reached before validation"
                )
            return _map_chunks_json(ids)

        inner = self._pipeline(
            findings=findings, needs_context=needs, on_validation=on_validation
        )

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "synthesis":
                raise PipelineDeadlineExceeded(
                    "provider cutoff reached before synthesis"
                )
            return inner(system, user)

        review, coverage, store, stats = _run_hierarchical(
            corpus, fake, validation_concurrency=1
        )
        self.assertTrue(coverage.complete)
        self.assertTrue(stats.deadline_exhausted)
        self.assertTrue(stats.validation_deadline_exhausted)
        self.assertEqual(stats.synthesis_calls, 0)
        _assert_unsynthesized_fallback(self, review)
        self.assertTrue(store.kept_findings())
        for finding in store.kept_findings():
            self.assertNotIn(finding.body, review["body"])
        self.assertIn(CANDIDATE_FINDINGS_NOT_POSTED, review["body"])

    def test_reduce_deadline_then_synthesis_timeout_posts_no_inline_comments(
        self,
    ) -> None:
        corpus, paths = self._paths_corpus(8)
        findings, needs = self._locatable_findings(paths)

        def on_reduce(stage: str, _user: str) -> str:
            if stage == "reduce":
                raise PipelineDeadlineExceeded(
                    "provider cutoff reached before reduce"
                )
            return json.dumps({"keep": [], "reject": [], "merge": []})

        inner = self._pipeline(
            findings=findings, needs_context=needs, on_reduce=on_reduce
        )

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "synthesis":
                raise PipelineDeadlineExceeded(
                    "provider cutoff reached before synthesis"
                )
            return inner(system, user)

        review, coverage, store, stats = _run_hierarchical(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertTrue(stats.deadline_exhausted)
        self.assertTrue(stats.reduce_deadline_exhausted)
        self.assertEqual(stats.synthesis_calls, 0)
        _assert_unsynthesized_fallback(self, review)
        self.assertTrue(store.kept_findings())
        self.assertIn(CANDIDATE_FINDINGS_NOT_POSTED, review["body"])

    def test_synthesis_provider_timeout_fail_closes_without_deadline_flag(
        self,
    ) -> None:
        corpus = build_review_corpus(_inputs(diff="@@ -1 +1 @@\n-a\n+b\n"))

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                return _map_chunks_json(
                    _chunk_ids_in_prompt(user),
                    findings=[
                        {
                            "id": "F1",
                            "severity": "MAJOR",
                            "path": "src/foo.c",
                            "side": "RIGHT",
                            "line": 1,
                            "body": "raw mapper candidate",
                            "confidence": "LIKELY",
                            "evidence": [],
                        }
                    ],
                )
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [_fid("F1")], "reject": [], "merge": []})
            raise ProviderRequestError(
                ProviderFailureKind.LATENCY_TIMEOUT,
                "xAI request timed out after 1 attempts",
            )

        review, coverage, store, stats = _run_hierarchical(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertFalse(stats.deadline_exhausted)
        self.assertEqual(stats.synthesis_calls, 0)
        _assert_unsynthesized_fallback(self, review)
        self.assertTrue(store.kept_findings())
        self.assertNotIn("raw mapper candidate", review["body"])
        self.assertIn(CANDIDATE_FINDINGS_NOT_POSTED, review["body"])

    def test_validation_deadline_with_successful_synthesis_keeps_inline_comments(
        self,
    ) -> None:
        corpus, paths = self._paths_corpus(4)
        findings, needs = self._locatable_findings(paths)
        synthesized = [
            {
                "path": "src/foo.c",
                "side": "RIGHT",
                "line": 1,
                "severity": "MAJOR",
                "body": "synthesized finding after incomplete validation",
            }
        ]
        clock = {"now": 10_000.0}
        provider_deadline = clock["now"] + 840.0
        inner = self._invoke_like(
            findings=findings,
            needs_context=needs,
            clock=clock,
            provider_deadline=provider_deadline,
            work={"map": 0.0, "validation": 10_000.0, "synthesis": 0.0},
        )

        def fake(system: str, user: str) -> str:
            raw = inner(system, user)
            if mw.provider_call_stage(system, user) == "synthesis":
                return json.dumps(
                    {
                        "event": "REQUEST_CHANGES",
                        "body": "# REQUEST CHANGES\n\nSynthesized review.\n",
                        "comments": synthesized,
                    }
                )
            return raw

        with mock.patch.object(rp.time, "monotonic", lambda: clock["now"]):
            review, coverage, _store, stats = _run_hierarchical(
                corpus, fake, deadline=provider_deadline, validation_concurrency=1
            )
        self.assertTrue(coverage.complete)
        self.assertFalse(stats.deadline_exhausted)
        self.assertTrue(stats.validation_deadline_exhausted)
        self.assertEqual(stats.synthesis_calls, 1)
        self.assertEqual(review["event"], "REQUEST_CHANGES")
        self.assertEqual(review["comments"], synthesized)


class MapSchedulerInvariantTests(unittest.TestCase):
    """Earlier stages may surrender unused time, but must never steal later reserves."""

    def _sized_chunks(self, sizes: list[int]) -> list[ContextChunk]:
        return [
            _chunk(f"C{index}", f"c{index}.c", "x" * size)
            for index, size in enumerate(sizes, 1)
        ]

    def test_map_cannot_consume_synthesis_reserve(self) -> None:
        chunks = self._sized_chunks([200] * 8)
        corpus = _synthetic_corpus(chunks)
        clock = {"now": 10_000.0}
        provider_deadline = clock["now"] + 840.0
        map_cutoff = map_stage_deadline(provider_deadline)
        fake = ValidationStageBudgetTests()._invoke_like(
            findings=[],
            needs_context=[],
            clock=clock,
            provider_deadline=provider_deadline,
            work={"map": 10_000.0, "pre-reduce": 0.0, "reduce": 0.0, "synthesis": 10.0},
        )
        with mock.patch.object(rp.time, "monotonic", lambda: clock["now"]):
            review, coverage, _store, stats = _run_hierarchical(
                corpus, fake, deadline=provider_deadline, map_concurrency=4
            )
        synthesis_started = fake.synthesis_started[0] - 10.0
        self.assertLessEqual(synthesis_started, provider_deadline)
        self.assertGreaterEqual(
            provider_deadline - synthesis_started, SYNTHESIS_RESERVE_SECONDS - 10.0
        )
        self.assertGreater(synthesis_started, map_cutoff - 0.001)
        self.assertTrue(stats.map_deadline_exhausted)
        self.assertFalse(stats.deadline_exhausted)
        self.assertEqual(stats.synthesis_calls, 1)
        self.assertNotEqual(review["event"].upper().replace(" ", "_"), "APPROVE")
        self.assertFalse(coverage.complete)

    def test_map_stage_timeout_continues_pipeline(self) -> None:
        chunks = _tiny_chunks(10)
        corpus = _synthetic_corpus(chunks)
        mapped: set[str] = set()
        synthesis_messages: list[str] = []

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                ids = _chunk_ids_in_prompt(user)
                if "C9" in ids:
                    raise StageDeadlineExceeded("map", "map allocation expired")
                mapped.update(ids)
                return _map_chunks_json(
                    ids,
                    findings=[
                        {
                            "id": "F1",
                            "severity": "MAJOR",
                            "path": "c1.c",
                            "side": "RIGHT",
                            "line": 1,
                            "body": "surviving mapper candidate",
                            "confidence": "LIKELY",
                        }
                    ],
                )
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            if stage == "synthesis":
                synthesis_messages.append(user)
                return json.dumps(
                    {
                        "event": "APPROVE",
                        "body": "# APPROVE\n\nShould be demoted.\n",
                        "comments": [],
                    }
                )
            return json.dumps({"keep": [], "reject": [], "merge": []})

        review, coverage, store, stats = _run_hierarchical(corpus, fake)
        self.assertTrue(stats.map_deadline_exhausted)
        self.assertFalse(stats.deadline_exhausted)
        self.assertTrue(mapped)
        self.assertTrue(coverage.uncovered_chunk_ids)
        self.assertTrue(set(mapped).isdisjoint(set(coverage.uncovered_chunk_ids)))
        self.assertEqual(stats.raw_finding_count, len(store.findings))
        self.assertGreater(stats.raw_finding_count, 0)
        self.assertEqual(stats.synthesis_calls, 1)
        self.assertEqual(len(synthesis_messages), 1)
        self.assertIn('"complete": false', synthesis_messages[0])
        self.assertNotEqual(review["event"].upper().replace(" ", "_"), "APPROVE")
        self.assertEqual(review.get("comments") or [], [])

    def test_global_provider_deadline_still_fail_closes(self) -> None:
        chunks = _tiny_chunks(8)
        corpus = _synthetic_corpus(chunks)
        later_stages = {"n": 0}

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                raise PipelineDeadlineExceeded("provider cutoff reached during map")
            later_stages["n"] += 1
            raise AssertionError("no later pipeline stage should run")

        review, coverage, store, stats = _run_hierarchical(
            corpus, fake, map_concurrency=2
        )
        self.assertEqual(later_stages["n"], 0)
        self.assertTrue(stats.deadline_exhausted)
        self.assertFalse(stats.map_deadline_exhausted)
        self.assertEqual(stats.raw_finding_count, len(store.findings))
        self.assertEqual(stats.raw_finding_count, 0)
        self.assertEqual(stats.synthesis_calls, 0)
        _assert_unsynthesized_fallback(self, review)
        self.assertFalse(coverage.complete)

    def test_slow_large_batch_splits_without_retrying_same_shape(self) -> None:
        chunks = _tiny_chunks(8)
        corpus = _synthetic_corpus(chunks)
        shapes: list[int] = []

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                ids = _chunk_ids_in_prompt(user)
                shapes.append(len(ids))
                if len(ids) >= 8:
                    raise RuntimeError("map call exceeded latency budget")
                return _map_chunks_json(ids)
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        _review, coverage, _store, stats = _run_hierarchical(corpus, fake)
        self.assertEqual(shapes.count(8), 1)
        self.assertIn(4, shapes)
        self.assertGreaterEqual(stats.map_batches_split, 1)
        self.assertTrue(coverage.complete)
        self.assertLess(shapes.count(8), 3)

    def test_early_transport_failure_retries_same_shape_once(self) -> None:
        chunks = _tiny_chunks(8)
        corpus = _synthetic_corpus(chunks)
        shapes: list[int] = []

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                ids = _chunk_ids_in_prompt(user)
                shapes.append(len(ids))
                if len(shapes) == 1:
                    raise ProviderRequestError(
                        ProviderFailureKind.TRANSIENT_TRANSPORT,
                        "connection reset after 0.5s",
                    )
                return _map_chunks_json(ids)
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        _review, coverage, _store, stats = _run_hierarchical(corpus, fake)
        self.assertEqual(shapes[:2], [8, 8])
        self.assertEqual(stats.map_batches_split, 0)
        self.assertTrue(coverage.complete)

    def test_repeated_slow_child_batches_continue_splitting(self) -> None:
        chunks = _tiny_chunks(8)
        corpus = _synthetic_corpus(chunks)
        shapes: list[int] = []

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                ids = _chunk_ids_in_prompt(user)
                shapes.append(len(ids))
                if len(ids) > 2:
                    raise ProviderRequestError(
                        ProviderFailureKind.LATENCY_TIMEOUT,
                        "map call latency budget exhausted",
                    )
                return _map_chunks_json(ids)
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        _review, coverage, _store, stats = _run_hierarchical(corpus, fake)
        self.assertEqual(shapes.count(8), 1)
        self.assertGreaterEqual(shapes.count(4), 2)
        self.assertIn(2, shapes)
        self.assertGreaterEqual(stats.map_batches_split, 3)
        self.assertTrue(coverage.complete)

    def test_single_chunk_latency_timeout_does_not_retry_forever(self) -> None:
        chunks = _tiny_chunks(1)
        corpus = _synthetic_corpus(chunks)
        shapes: list[int] = []

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                ids = _chunk_ids_in_prompt(user)
                shapes.append(len(ids))
                raise ProviderRequestError(
                    ProviderFailureKind.LATENCY_TIMEOUT,
                    "map call latency budget exhausted",
                )
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        _review, coverage, _store, stats = _run_hierarchical(corpus, fake)
        # Retries are bounded by the per-chunk ceiling, then the chunk is
        # abandoned and coverage stays incomplete.
        self.assertEqual(shapes, [1] * (MAP_MISSING_CHUNK_RETRIES + 1))
        self.assertFalse(coverage.complete)
        self.assertEqual(stats.map_attempts, MAP_MISSING_CHUNK_RETRIES + 1)

    def test_single_chunk_transport_failure_retries_until_ceiling(self) -> None:
        chunks = _tiny_chunks(1)
        corpus = _synthetic_corpus(chunks)
        shapes: list[int] = []

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                ids = _chunk_ids_in_prompt(user)
                shapes.append(len(ids))
                raise ProviderRequestError(
                    ProviderFailureKind.TRANSIENT_TRANSPORT,
                    "connection reset after 0.5s",
                )
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        _review, coverage, _store, stats = _run_hierarchical(corpus, fake)
        # A single chunk has nothing to split into, so the transport ceiling is
        # terminal: there is no handoff to the missing-chunk floor below it.
        self.assertEqual(len(shapes), MAP_TRANSPORT_RETRIES + 1)
        self.assertEqual(set(shapes), {1})
        self.assertFalse(coverage.complete)
        self.assertEqual(stats.map_attempts, MAP_TRANSPORT_RETRIES + 1)

    def test_capacity_rejection_retries_the_same_shape_instead_of_splitting(
        self,
    ) -> None:
        """Capacity says nothing about size, so halving the request is wrong."""
        chunks = _tiny_chunks(4)
        corpus = _synthetic_corpus(chunks)
        clock = {"now": 10_000.0}
        shapes: list[int] = []

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                ids = _chunk_ids_in_prompt(user)
                shapes.append(len(ids))
                if len(shapes) == 1:
                    raise ProviderRequestError(
                        ProviderFailureKind.CAPACITY,
                        "Gemini HTTP 503: high demand",
                    )
                return _map_chunks_json(ids)
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        def fake_sleep(seconds: float) -> None:
            clock["now"] += seconds

        with mock.patch.object(rp.time, "monotonic", lambda: clock["now"]):
            with mock.patch.object(rp.time, "sleep", fake_sleep):
                _review, coverage, _store, stats = _run_hierarchical(corpus, fake)
        self.assertEqual(shapes, [4, 4])
        self.assertEqual(stats.map_batches_split, 0)
        self.assertEqual(stats.map_capacity_retries, 1)
        self.assertTrue(coverage.complete)

    def test_child_batches_are_not_started_after_map_cutoff(self) -> None:
        chunks = _tiny_chunks(8)
        corpus = _synthetic_corpus(chunks)
        clock = {"now": 10_000.0}
        provider_deadline = clock["now"] + 840.0
        map_cutoff = map_stage_deadline(provider_deadline)
        started: list[int] = []

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                ids = _chunk_ids_in_prompt(user)
                started.append(len(ids))
                if len(ids) >= 8:
                    clock["now"] = map_cutoff
                    raise RuntimeError("map call exceeded latency budget")
                raise AssertionError("child map calls must not start at cutoff")
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n\nPartial.\n", "comments": []}
            )

        with mock.patch.object(rp.time, "monotonic", lambda: clock["now"]):
            _review, _coverage, _store, stats = _run_hierarchical(
                corpus, fake, deadline=provider_deadline
            )
        self.assertEqual(started, [8])
        self.assertTrue(stats.map_deadline_exhausted)
        self.assertEqual(stats.synthesis_calls, 1)

    def test_child_batches_are_not_started_without_a_full_call_budget(self) -> None:
        chunks = _tiny_chunks(8)
        corpus = _synthetic_corpus(chunks)
        clock = {"now": 10_000.0}
        provider_deadline = clock["now"] + 840.0
        map_cutoff = map_stage_deadline(provider_deadline)
        started: list[int] = []

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                ids = _chunk_ids_in_prompt(user)
                started.append(len(ids))
                if len(ids) >= 8:
                    clock["now"] = map_cutoff - (MAP_CALL_BUDGET_SECONDS - 1.0)
                    raise RuntimeError("map call exceeded latency budget")
                raise AssertionError(
                    "child map calls must not start without a full call budget"
                )
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n\nPartial.\n", "comments": []}
            )

        with mock.patch.object(rp.time, "monotonic", lambda: clock["now"]):
            _review, _coverage, _store, stats = _run_hierarchical(
                corpus, fake, deadline=provider_deadline
            )
        self.assertEqual(started, [8])
        self.assertTrue(stats.map_deadline_exhausted)
        self.assertEqual(stats.synthesis_calls, 1)

    def test_raw_finding_count_on_map_stage_exit(self) -> None:
        chunks = _tiny_chunks(10)
        corpus = _synthetic_corpus(chunks)

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                ids = _chunk_ids_in_prompt(user)
                if "C9" in ids:
                    raise StageDeadlineExceeded("map", "map allocation expired")
                return _map_chunks_json(
                    ids,
                    findings=[
                        {
                            "id": "F1",
                            "severity": "MAJOR",
                            "path": "c1.c",
                            "side": "RIGHT",
                            "line": 1,
                            "body": "first finding",
                            "confidence": "LIKELY",
                        },
                        {
                            "id": "F2",
                            "severity": "MINOR",
                            "path": "c1.c",
                            "side": "RIGHT",
                            "line": 1,
                            "body": "second finding",
                            "confidence": "QUESTION",
                        },
                    ],
                )
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n\nPartial.\n", "comments": []}
            )

        _review, _coverage, store, stats = _run_hierarchical(corpus, fake)
        self.assertEqual(stats.raw_finding_count, len(store.findings))
        self.assertEqual(stats.raw_finding_count, 2)
        self.assertIn(f"{stats.raw_finding_count} raw finding(s)", stats.footer())
        self.assertIn("map budget exhausted", stats.footer())

    def test_zero_findings_stays_zero_on_map_stage_exit(self) -> None:
        chunks = _tiny_chunks(10)
        corpus = _synthetic_corpus(chunks)

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                ids = _chunk_ids_in_prompt(user)
                if "C9" in ids:
                    raise StageDeadlineExceeded("map", "map allocation expired")
                return _map_chunks_json(ids)
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        review, coverage, store, stats = _run_hierarchical(corpus, fake)
        self.assertEqual(len(store.findings), 0)
        self.assertEqual(stats.raw_finding_count, 0)
        self.assertTrue(stats.map_deadline_exhausted)
        self.assertNotEqual(review["event"].upper().replace(" ", "_"), "APPROVE")
        self.assertFalse(coverage.complete)

    def test_unsynthesized_mapper_candidates_are_not_posted(self) -> None:
        chunks = _tiny_chunks(2)
        corpus = _synthetic_corpus(chunks)

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system:
                ids = _chunk_ids_in_prompt(user)
                return _map_chunks_json(
                    ids,
                    findings=[
                        {
                            "id": "F1",
                            "severity": "MAJOR",
                            "path": "c1.c",
                            "side": "RIGHT",
                            "line": 1,
                            "body": "must not become an inline comment",
                            "confidence": "LIKELY",
                        }
                    ],
                )
            raise PipelineDeadlineExceeded("provider cutoff reached before synthesis")

        review, _coverage, store, stats = _run_hierarchical(corpus, fake)
        self.assertTrue(stats.deadline_exhausted)
        self.assertEqual(stats.synthesis_calls, 0)
        _assert_unsynthesized_fallback(self, review)
        self.assertTrue(store.findings)
        self.assertNotIn("must not become an inline comment", review["body"])
        self.assertEqual(review["comments"], [])

    def test_hard_request_limit_still_wins_with_balanced_packing(self) -> None:
        chunks = [
            _chunk(
                f"file:src/p{i}.c:1",
                f"src/p{i}.c",
                f"int f{i}(void) {{ return {i}; }}\n" + ("A" * 80),
            )
            for i in range(6)
        ]
        index = "Changed files:\n" + "\n".join(
            f"{i}. src/p{i}.c +10 -2" for i in range(40)
        ) + "\n"
        corpus = _synthetic_corpus(chunks, index=index)
        reviewable = corpus.reviewable_chunks
        single_max = max(
            len(format_map_user_message(corpus, [chunk])) for chunk in reviewable
        )
        recorder = _ReviewRecorder()
        _review, _coverage, _store, stats = _run_hierarchical(
            corpus,
            recorder,
            max_map_request_chars=single_max,
            map_overhead_chars=24_000,
        )
        for message in recorder.map_messages:
            self.assertLessEqual(len(message), single_max)
        self.assertGreaterEqual(stats.map_attempts, 1)


class ReducerDecisionValidationTests(unittest.TestCase):
    """Reducer JSON is untrusted; only current group_ids may mutate state."""

    def _store(self, *findings: Finding) -> EvidenceStore:
        store = EvidenceStore()
        for finding in findings:
            store.findings[finding.id] = finding
        return store

    def _assert_known_identities(self, store: EvidenceStore) -> None:
        known = set(store.findings)
        self.assertLessEqual(store.kept, known)
        self.assertLessEqual(set(store.rejected), known)
        self.assertLessEqual(set(store.merged_into), known)
        self.assertLessEqual(set(store.merged_into.values()), known)

    def test_hallucinated_keep_id_is_ignored(self) -> None:
        store = self._store(_finding("A"), _finding("B"))
        raw = json.dumps({"keep": ["NOT_REAL"], "reject": [], "merge": []})
        apply_reduce_decision(store, raw, ["A", "B"])
        self.assertNotIn("NOT_REAL", store.kept)
        self.assertIn("A", store.kept)
        self.assertIn("B", store.kept)
        self._assert_known_identities(store)

    def test_hallucinated_reject_id_is_ignored(self) -> None:
        store = self._store(_finding("A"), _finding("B"))
        raw = json.dumps(
            {
                "keep": [],
                "reject": [{"id": "NOT_REAL", "reason": "because model said so"}],
                "merge": [],
            }
        )
        apply_reduce_decision(store, raw, ["A", "B"])
        self.assertNotIn("NOT_REAL", store.rejected)
        self.assertIn("A", store.kept)
        self.assertIn("B", store.kept)
        self._assert_known_identities(store)

    def test_merge_with_hallucinated_canonical_is_rejected(self) -> None:
        store = self._store(_finding("A"), _finding("B"))
        raw = json.dumps(
            {
                "keep": [],
                "reject": [],
                "merge": [{"ids": ["A", "B"], "canonical": "NOT_REAL"}],
            }
        )
        apply_reduce_decision(store, raw, ["A", "B"])
        self.assertIn("A", store.kept)
        self.assertIn("B", store.kept)
        self.assertNotIn("A", store.merged_into)
        self.assertNotIn("B", store.merged_into)
        self.assertNotIn("NOT_REAL", store.kept)
        self.assertFalse(store.merged_into)
        self._assert_known_identities(store)

    def test_merge_canonical_must_be_a_merge_member(self) -> None:
        store = self._store(_finding("A"), _finding("B"), _finding("C"))
        raw = json.dumps(
            {
                "keep": [],
                "reject": [],
                "merge": [{"ids": ["A", "B"], "canonical": "C"}],
            }
        )
        apply_reduce_decision(store, raw, ["A", "B", "C"])
        self.assertIn("A", store.kept)
        self.assertIn("B", store.kept)
        self.assertIn("C", store.kept)
        self.assertNotIn("A", store.merged_into)
        self.assertNotIn("B", store.merged_into)
        self.assertFalse(store.merged_into)
        self._assert_known_identities(store)

    def test_foreign_real_finding_cannot_be_merge_canonical(self) -> None:
        store = self._store(_finding("A"), _finding("B"), _finding("C"))
        raw = json.dumps(
            {
                "keep": [],
                "reject": [],
                "merge": [{"ids": ["A", "B"], "canonical": "C"}],
            }
        )
        apply_reduce_decision(store, raw, ["A", "B"])
        self.assertIn("A", store.kept)
        self.assertIn("B", store.kept)
        self.assertNotIn("C", store.kept)
        self.assertNotIn("A", store.merged_into)
        self.assertNotIn("B", store.merged_into)
        self.assertNotIn("C", store.merged_into)
        self.assertNotIn("C", store.merged_into.values())
        self._assert_known_identities(store)

    def test_foreign_real_finding_cannot_be_merge_member(self) -> None:
        store = self._store(_finding("A"), _finding("B"), _finding("C"))
        raw = json.dumps(
            {
                "keep": [],
                "reject": [],
                "merge": [{"ids": ["A", "C"], "canonical": "A"}],
            }
        )
        apply_reduce_decision(store, raw, ["A", "B"])
        self.assertIn("A", store.kept)
        self.assertIn("B", store.kept)
        self.assertNotIn("C", store.kept)
        self.assertNotIn("C", store.merged_into)
        self.assertNotIn("C", store.merged_into.values())
        self.assertFalse(store.merged_into)
        self._assert_known_identities(store)

    def test_valid_merge_still_works(self) -> None:
        store = self._store(_finding("A"), _finding("B"))
        raw = json.dumps(
            {
                "keep": [],
                "reject": [],
                "merge": [{"ids": ["A", "B"], "canonical": "A"}],
            }
        )
        apply_reduce_decision(store, raw, ["A", "B"])
        self.assertIn("A", store.kept)
        self.assertEqual(store.merged_into["B"], "A")
        self.assertNotIn("B", store.kept)
        kept_ids = {finding.id for finding in store.kept_findings()}
        self.assertEqual(kept_ids, {"A"})
        self._assert_known_identities(store)

    def test_valid_reject_still_works(self) -> None:
        store = self._store(_finding("A"), _finding("B"))
        raw = json.dumps(
            {
                "reject": [{"id": "A", "reason": "Contradicted by C7"}],
                "keep": ["B"],
                "merge": [],
            }
        )
        apply_reduce_decision(store, raw, ["A", "B"])
        self.assertIn("A", store.rejected)
        self.assertEqual(store.rejected["A"], "Contradicted by C7")
        self.assertIn("B", store.kept)
        self.assertNotIn("A", store.kept)
        self._assert_known_identities(store)

    def test_malformed_canonical_fails_safe_to_keep(self) -> None:
        a = "diff:a.c:1/F1"
        b = "diff:b.c:1/F1"
        store = self._store(_finding(a), _finding(b))
        raw = json.dumps(
            {
                "keep": [],
                "reject": [],
                "merge": [{"ids": [a, b], "canonical": "F1"}],
            }
        )
        apply_reduce_decision(store, raw, [a, b])
        self.assertIn(a, store.kept)
        self.assertIn(b, store.kept)
        self.assertFalse(store.merged_into)
        self.assertNotIn("F1", store.kept)
        self._assert_known_identities(store)

    def test_invalid_canonical_cannot_erase_blocking_finding(self) -> None:
        a = "diff:a.c:1/F1"
        b = "diff:b.c:1/F1"
        store = self._store(
            _finding(a, severity="MINOR"),
            _finding(b, severity="BLOCKING"),
        )
        raw = json.dumps(
            {
                "keep": [],
                "reject": [],
                "merge": [{"ids": [a, b], "canonical": "F1"}],
            }
        )
        apply_reduce_decision(store, raw, [a, b])
        kept_ids = {finding.id for finding in store.kept_findings()}
        self.assertIn(a, kept_ids)
        self.assertIn(b, kept_ids)
        self.assertEqual(
            {finding.severity for finding in store.kept_findings()},
            {"MINOR", "BLOCKING"},
        )
        self._assert_known_identities(store)

    def test_reducer_cannot_mutate_finding_outside_its_group(self) -> None:
        store = self._store(_finding("A"), _finding("B"), _finding("C"))
        store.kept.add("C")
        before = {
            "finding": deepcopy(store.findings["C"]),
            "kept": set(store.kept),
            "rejected": dict(store.rejected),
            "merged_into": dict(store.merged_into),
        }
        raw = json.dumps(
            {
                "keep": [],
                "reject": [{"id": "C", "reason": "foreign reject"}],
                "merge": [{"ids": ["A", "B"], "canonical": "C"}],
            }
        )
        apply_reduce_decision(store, raw, ["A", "B"])
        self.assertEqual(store.findings["C"], before["finding"])
        self.assertEqual("C" in store.kept, "C" in before["kept"])
        self.assertEqual(
            {key: value for key, value in store.rejected.items() if key == "C"},
            {key: value for key, value in before["rejected"].items() if key == "C"},
        )
        self.assertEqual(
            {
                key: value
                for key, value in store.merged_into.items()
                if key == "C" or value == "C"
            },
            {
                key: value
                for key, value in before["merged_into"].items()
                if key == "C" or value == "C"
            },
        )
        self.assertIn("C", store.kept)
        self.assertIn("A", store.kept)
        self.assertIn("B", store.kept)
        self.assertFalse(store.merged_into)
        self._assert_known_identities(store)

    def test_keep_and_reject_conflict_fails_safe_to_keep(self) -> None:
        """One response may not both keep and reject the same finding.

        Conflicting actions are ignored so the finding defaults to KEEP.
        """
        store = self._store(_finding("A"), _finding("B"))
        raw = json.dumps(
            {
                "keep": ["A"],
                "reject": [{"id": "A", "reason": "contradiction"}],
                "merge": [],
            }
        )
        apply_reduce_decision(store, raw, ["A", "B"])
        self.assertIn("A", store.kept)
        self.assertNotIn("A", store.rejected)
        self.assertIn("B", store.kept)
        self._assert_known_identities(store)

    def test_reject_and_merge_conflict_fails_safe_to_keep(self) -> None:
        store = self._store(_finding("A"), _finding("B"))
        raw = json.dumps(
            {
                "keep": [],
                "reject": [{"id": "A", "reason": "bad"}],
                "merge": [{"ids": ["A", "B"], "canonical": "B"}],
            }
        )
        apply_reduce_decision(store, raw, ["A", "B"])
        self.assertIn("A", store.kept)
        self.assertIn("B", store.kept)
        self.assertNotIn("A", store.rejected)
        self.assertFalse(store.merged_into)
        self._assert_known_identities(store)

    def test_single_member_merge_is_ignored(self) -> None:
        store = self._store(_finding("A"), _finding("B"))
        raw = json.dumps(
            {
                "keep": [],
                "reject": [],
                "merge": [{"ids": ["A"], "canonical": "A"}],
            }
        )
        apply_reduce_decision(store, raw, ["A", "B"])
        self.assertIn("A", store.kept)
        self.assertIn("B", store.kept)
        self.assertFalse(store.merged_into)
        self._assert_known_identities(store)

    def test_overlapping_merges_fail_safe_to_keep(self) -> None:
        store = self._store(_finding("A"), _finding("B"), _finding("C"))
        raw = json.dumps(
            {
                "keep": [],
                "reject": [],
                "merge": [
                    {"ids": ["A", "B"], "canonical": "A"},
                    {"ids": ["B", "C"], "canonical": "C"},
                ],
            }
        )
        apply_reduce_decision(store, raw, ["A", "B", "C"])
        self.assertIn("A", store.kept)
        self.assertIn("B", store.kept)
        self.assertIn("C", store.kept)
        self.assertFalse(store.merged_into)
        self._assert_known_identities(store)


class SeverityCanonicalizationTests(unittest.TestCase):
    """Mapper aliases must share posting's BLOCKING | MAJOR | MINOR vocabulary."""

    def _parse(self, **raw: object) -> Finding:
        payload = {"body": "defect", **raw}
        parsed = rp._parse_finding(payload, "diff:a.c:1", set(), {})
        self.assertIsNotNone(parsed)
        assert parsed is not None
        return parsed

    def test_canonical_severity_maps_blocker_alias_and_unknowns(self) -> None:
        self.assertEqual(rp.canonical_severity("blocker"), "BLOCKING")
        self.assertEqual(rp.canonical_severity("BLOCKER"), "BLOCKING")
        self.assertEqual(rp.canonical_severity("blocking"), "BLOCKING")
        self.assertEqual(rp.canonical_severity("MAJOR"), "MAJOR")
        self.assertEqual(rp.canonical_severity("CRITICAL"), "MINOR")
        self.assertEqual(rp.canonical_severity("HIGH"), "MINOR")
        self.assertEqual(rp.canonical_severity(""), "MINOR")
        self.assertEqual(rp.canonical_severity(None), "MINOR")
        self.assertEqual(rp.canonical_severity("suggestion"), "MINOR")

    def test_parse_finding_stores_canonical_severity(self) -> None:
        self.assertEqual(self._parse(severity="blocker").severity, "BLOCKING")
        self.assertEqual(self._parse(severity="BLOCKER").severity, "BLOCKING")
        self.assertEqual(self._parse(severity="CRITICAL").severity, "MINOR")
        self.assertEqual(self._parse().severity, "MINOR")
        self.assertEqual(self._parse(severity="").severity, "MINOR")

    def test_join_severity_blocker_beats_minor_and_returns_blocking(self) -> None:
        blocker = _finding("b", severity="BLOCKER")
        minor = _finding("m", severity="MINOR")
        self.assertEqual(join_severity([blocker, minor]), "BLOCKING")
        self.assertEqual(join_severity([minor, blocker]), "BLOCKING")
        self.assertEqual(join_severity([blocker]), "BLOCKING")


class ReducerEvidentiaryStrengthTests(unittest.TestCase):
    """Valid merges must not weaken the surviving representative."""

    def _store(self, *findings: Finding) -> EvidenceStore:
        store = EvidenceStore()
        for finding in findings:
            store.findings[finding.id] = finding
        return store

    def _apply_merge(
        self,
        store: EvidenceStore,
        ids: list[str],
        canonical: str,
        group_ids: list[str] | None = None,
    ) -> None:
        raw = json.dumps(
            {
                "keep": [],
                "reject": [],
                "merge": [{"ids": ids, "canonical": canonical}],
            }
        )
        apply_reduce_decision(store, raw, group_ids or ids)

    def _sole_kept(self, store: EvidenceStore) -> Finding:
        kept = store.kept_findings()
        self.assertEqual(len(kept), 1, [item.id for item in kept])
        return kept[0]

    def test_valid_merge_cannot_downgrade_blocking_to_minor(self) -> None:
        store = self._store(
            _finding("A", severity="MINOR"),
            _finding("B", severity="BLOCKING"),
        )
        self._apply_merge(store, ["A", "B"], "A")
        result = self._sole_kept(store)
        self.assertEqual(result.id, "A")
        self.assertEqual(result.severity, "BLOCKING")

    def test_canonical_choice_cannot_affect_severity(self) -> None:
        severities = []
        for canonical in ("A", "B"):
            store = self._store(
                _finding("A", severity="MINOR"),
                _finding("B", severity="BLOCKING"),
            )
            self._apply_merge(store, ["A", "B"], canonical)
            result = self._sole_kept(store)
            self.assertEqual(result.id, canonical)
            severities.append(result.severity)
        self.assertEqual(severities, ["BLOCKING", "BLOCKING"])

    def test_valid_merge_preserves_all_evidence(self) -> None:
        store = self._store(
            _finding("A", evidence=["evidence:a"]),
            _finding("B", evidence=["evidence:b"]),
        )
        self._apply_merge(store, ["A", "B"], "A")
        result = self._sole_kept(store)
        self.assertGreaterEqual(set(result.evidence), {"evidence:a", "evidence:b"})

    def test_valid_merge_cannot_erase_incomplete_validation(self) -> None:
        store = self._store(
            _finding("A", evidence=["chunk:a"]),
            _finding(
                "B",
                evidence=["chunk:b", "validation:incomplete:include/foo.h"],
            ),
        )
        self._apply_merge(store, ["A", "B"], "A")
        result = self._sole_kept(store)
        self.assertIn("validation:incomplete:include/foo.h", result.evidence)

    def test_canonical_choice_cannot_affect_evidence_union(self) -> None:
        evidence_sets = []
        for canonical in ("A", "B"):
            store = self._store(
                _finding("A", evidence=["evidence:a"]),
                _finding("B", evidence=["evidence:b"]),
            )
            self._apply_merge(store, ["A", "B"], canonical)
            evidence_sets.append(set(self._sole_kept(store).evidence))
        self.assertEqual(evidence_sets[0], evidence_sets[1])
        self.assertGreaterEqual(evidence_sets[0], {"evidence:a", "evidence:b"})

    def test_three_way_merge_preserves_strongest_severity_and_all_evidence(self) -> None:
        store = self._store(
            _finding("A", severity="MINOR", evidence=["evidence:a"]),
            _finding("B", severity="MAJOR", evidence=["evidence:b"]),
            _finding("C", severity="BLOCKING", evidence=["evidence:c"]),
        )
        self._apply_merge(store, ["A", "B", "C"], "A")
        result = self._sole_kept(store)
        self.assertEqual(result.id, "A")
        self.assertEqual(result.severity, "BLOCKING")
        self.assertGreaterEqual(
            set(result.evidence),
            {"evidence:a", "evidence:b", "evidence:c"},
        )

    def test_transitive_merge_preserves_all_metadata(self) -> None:
        store = self._store(
            _finding("A", severity="MINOR", evidence=["evidence:a"]),
            _finding("B", severity="BLOCKING", evidence=["evidence:b"]),
            _finding("C", severity="MAJOR", evidence=["evidence:c"]),
        )
        self._apply_merge(store, ["A", "B"], "A")
        self._apply_merge(store, ["A", "C"], "C")
        result = self._sole_kept(store)
        self.assertEqual(result.id, "C")
        self.assertEqual(result.severity, "BLOCKING")
        self.assertGreaterEqual(
            set(result.evidence),
            {"evidence:a", "evidence:b", "evidence:c"},
        )

    def test_incomplete_marker_survives_transitive_merge(self) -> None:
        store = self._store(
            _finding("A", evidence=["chunk:a"]),
            _finding("B", evidence=["validation:incomplete:foo.h"]),
            _finding("C", evidence=["chunk:c"]),
        )
        self._apply_merge(store, ["A", "B"], "A")
        self._apply_merge(store, ["A", "C"], "C")
        result = self._sole_kept(store)
        self.assertEqual(result.id, "C")
        self.assertIn("validation:incomplete:foo.h", result.evidence)

    def test_kept_findings_is_idempotent(self) -> None:
        store = self._store(
            _finding("A", severity="MINOR", evidence=["evidence:a"]),
            _finding(
                "B",
                severity="BLOCKING",
                evidence=["evidence:b", "validation:incomplete:foo.h"],
            ),
        )
        self._apply_merge(store, ["A", "B"], "A")
        before = {
            "findings": deepcopy(store.findings),
            "kept": set(store.kept),
            "rejected": dict(store.rejected),
            "merged_into": dict(store.merged_into),
        }
        first = store.kept_findings()
        second = store.kept_findings()
        self.assertEqual(first, second)
        self.assertEqual(store.findings, before["findings"])
        self.assertEqual(store.kept, before["kept"])
        self.assertEqual(store.rejected, before["rejected"])
        self.assertEqual(store.merged_into, before["merged_into"])

    def test_raw_findings_remain_unchanged(self) -> None:
        store = self._store(
            _finding("A", severity="MINOR", evidence=["evidence:a"]),
            _finding("B", severity="BLOCKING", evidence=["evidence:b"]),
        )
        original_a = deepcopy(store.findings["A"])
        original_b = deepcopy(store.findings["B"])
        self._apply_merge(store, ["A", "B"], "A")
        store.kept_findings()
        self.assertEqual(store.findings["A"], original_a)
        self.assertEqual(store.findings["B"], original_b)

    def test_rejected_findings_do_not_leak_into_unrelated_representatives(self) -> None:
        store = self._store(
            _finding("A", severity="MINOR", evidence=["evidence:a"]),
            _finding("B", severity="MAJOR", evidence=["evidence:b"]),
            _finding(
                "C",
                severity="BLOCKING",
                evidence=["evidence:c", "validation:incomplete:foo.h"],
            ),
            _finding("D", severity="MINOR", evidence=["evidence:d"]),
        )
        self._apply_merge(store, ["A", "B"], "A")
        apply_reduce_decision(
            store,
            json.dumps(
                {
                    "keep": ["D"],
                    "reject": [{"id": "C", "reason": "independently contradicted"}],
                    "merge": [],
                }
            ),
            ["C", "D"],
        )
        # Stale merge pointer from the rejected finding must not contribute.
        store.merged_into["C"] = "A"
        kept = {item.id: item for item in store.kept_findings()}
        self.assertEqual(set(kept), {"A", "D"})
        self.assertEqual(kept["A"].severity, "MAJOR")
        self.assertGreaterEqual(set(kept["A"].evidence), {"evidence:a", "evidence:b"})
        self.assertNotIn("evidence:c", kept["A"].evidence)
        self.assertNotIn("validation:incomplete:foo.h", kept["A"].evidence)
        self.assertEqual(kept["D"].severity, "MINOR")
        self.assertNotIn("evidence:c", kept["D"].evidence)
        self.assertNotIn("C", {item.id for item in store.kept_findings()})

    def test_canonical_choice_cannot_affect_confidence(self) -> None:
        confidences = []
        for canonical in ("A", "B"):
            store = self._store(
                _finding("A", confidence="CONFIRMED", evidence=["evidence:a"]),
                _finding("B", confidence="QUESTION", evidence=["evidence:b"]),
            )
            self._apply_merge(store, ["A", "B"], canonical)
            result = self._sole_kept(store)
            self.assertEqual(result.id, canonical)
            confidences.append(result.confidence)
        self.assertEqual(confidences, ["CONFIRMED", "CONFIRMED"])

    def test_confidence_join_is_independent_of_canonical_and_preserves_markers(self) -> None:
        store = self._store(
            _finding("A", confidence="CONFIRMED", evidence=["chunk:a"]),
            _finding(
                "B",
                confidence="LIKELY",
                evidence=["chunk:b", "validation:incomplete:include/foo.h"],
            ),
        )
        self._apply_merge(store, ["A", "B"], "A")
        result = self._sole_kept(store)
        self.assertEqual(result.confidence, "CONFIRMED")
        self.assertIn("validation:incomplete:include/foo.h", result.evidence)


class ReducerTerminationTests(unittest.TestCase):
    def test_reducer_terminates_when_all_findings_survive(self) -> None:
        store = EvidenceStore()
        count = REDUCE_GROUP_SIZE * 2
        for index in range(1, count + 1):
            store.findings[f"F{index}"] = _finding(f"F{index}")
        stats = PipelineStats()

        def keep_all(_system: str, user: str) -> str:
            ids = _reduce_payload_ids(user)
            return json.dumps({"keep": ids, "reject": [], "merge": []})

        hierarchical_reduce(store, "<!-- merge-warden-reduce -->", keep_all, 50_000, stats)
        # Round 1: two groups. Round 2: same two groups, then fixed point
        # (survivors still do not fit in one judge call).
        self.assertEqual(stats.reduce_calls, 4)
        self.assertEqual(
            {item.id for item in store.kept_findings()},
            {f"F{index}" for index in range(1, count + 1)},
        )
        self.assertTrue(any("fixed point" in note for note in stats.notes))

    def test_reducer_still_merges_and_rejects(self) -> None:
        store = EvidenceStore()
        for index in range(1, 11):
            store.findings[f"F{index}"] = _finding(f"F{index}")
        stats = PipelineStats()

        def fake(_system: str, user: str) -> str:
            ids = _reduce_payload_ids(user)
            if {"F1", "F2", "F3"}.issubset(ids):
                return json.dumps(
                    {
                        "keep": [item for item in ids if item not in {"F1", "F2", "F3"}],
                        "reject": [{"id": "F3", "reason": "contradicted"}],
                        "merge": [{"ids": ["F1", "F2"], "canonical": "F1"}],
                    }
                )
            return json.dumps({"keep": ids, "reject": [], "merge": []})

        hierarchical_reduce(store, "<!-- merge-warden-reduce -->", fake, 50_000, stats)
        kept_ids = {item.id for item in store.kept_findings()}
        self.assertIn("F1", kept_ids)
        self.assertNotIn("F2", kept_ids)
        self.assertNotIn("F3", kept_ids)
        self.assertEqual(store.merged_into.get("F2"), "F1")
        self.assertIn("F3", store.rejected)
        self.assertEqual(len(kept_ids), 8)
        self.assertGreater(stats.reduce_calls, 0)
        self.assertLess(stats.reduce_calls, 100)

    def test_reducer_stops_at_max_rounds(self) -> None:
        store = EvidenceStore()
        count = REDUCE_GROUP_SIZE + MAX_REDUCE_ROUNDS + 1
        for index in range(1, count + 1):
            store.findings[f"F{index}"] = _finding(f"F{index}")
        stats = PipelineStats()
        previous_first = {"n": 0}

        def shrink(_system: str, user: str) -> str:
            ids = _reduce_payload_ids(user)
            first_num = int(ids[0][1:])
            new_round = previous_first["n"] == 0 or first_num <= previous_first["n"]
            previous_first["n"] = first_num
            if new_round:
                return json.dumps(
                    {
                        "keep": ids[1:],
                        "reject": [{"id": ids[0], "reason": "cap-test"}],
                        "merge": [],
                    }
                )
            return json.dumps({"keep": ids, "reject": [], "merge": []})

        hierarchical_reduce(store, "<!-- merge-warden-reduce -->", shrink, 50_000, stats)
        self.assertTrue(any("stopped after" in note for note in stats.notes))
        self.assertLessEqual(stats.reduce_calls, MAX_REDUCE_ROUNDS * ((count + REDUCE_GROUP_SIZE - 1) // REDUCE_GROUP_SIZE))
        self.assertGreater(stats.reduce_calls, 0)
        kept = store.kept_findings()
        self.assertTrue(kept)
        self.assertGreater(len(kept), REDUCE_GROUP_SIZE)
        self.assertLess(len(kept), count)

    def test_reducer_rejudges_survivors_that_fit_after_first_round_merges(self) -> None:
        """A full map batch of identical findings must become one canonical.

        First-round groups of 5 and 3 each collapse to one survivor. Stopping
        there would leave two canonicals that never sat in the same judge
        call. The tournament continues until those leftovers are co-judged.
        """
        store = EvidenceStore()
        count = REDUCE_GROUP_SIZE + 3
        body = "same TYPE_NAME lexer/parser defect"
        for index in range(1, count + 1):
            store.findings[f"F{index}"] = _finding(f"F{index}", body=body)
        stats = PipelineStats()

        hierarchical_reduce(
            store,
            "<!-- merge-warden-reduce -->",
            lambda _system, user: _merge_equivalent_reduce(user),
            50_000,
            stats,
        )
        kept = store.kept_findings()
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].body, body)
        self.assertEqual(stats.reduce_calls, 3)
        self.assertGreaterEqual(len(store.merged_into), count - 1)

    def test_run_pre_reduce_collapses_identical_bodies_before_validation(self) -> None:
        store = EvidenceStore()
        count = REDUCE_GROUP_SIZE + 3
        body = "same TYPE_NAME lexer/parser defect"
        for index in range(1, count + 1):
            finding_id = f"diff:src/lang_{index}.c:1/F1"
            store.findings[finding_id] = _finding(
                finding_id,
                body=body,
                evidence=[f"chunk:diff:src/lang_{index}.c:1"],
            )
            store.needs_context.append(
                rp.ContextNeed(
                    path="src/parser.y" if index % 2 == 0 else "src/lexer.l",
                    reason="Need TYPE_NAME invariant",
                    from_chunk=f"diff:src/lang_{index}.c:1",
                    finding_ids=[finding_id],
                )
            )
        stats = PipelineStats()
        run_pre_reduce(
            store,
            "<!-- merge-warden-reduce -->",
            lambda _system, user: _merge_equivalent_reduce(user),
            50_000,
            stats,
        )
        self.assertEqual(stats.raw_finding_count, count)
        self.assertEqual(stats.reduced_finding_count, 1)
        self.assertEqual(stats.validation_attempts, 0)
        kept = store.kept_findings()
        self.assertEqual(len(kept), 1)
        canonical = kept[0].id
        self.assertTrue(store.needs_context)
        self.assertEqual({need.finding_ids[0] for need in store.needs_context}, {canonical})
        self.assertEqual(
            {need.path for need in store.needs_context},
            {"src/lexer.l", "src/parser.y"},
        )


class RequestPlannerTests(unittest.TestCase):
    def test_plan_requests_splits_on_rendered_size(self) -> None:
        chunks = [_chunk(f"c{i}", "a.c", f"CHUNK-{i}-" + ("x" * 40)) for i in range(4)]

        def render(batch: list[ContextChunk]) -> str:
            return "HDR" + "".join(item.text for item in batch)

        overhead = 3
        limit = overhead + 90
        plan = plan_requests(chunks, render, limit)
        packed = [item for batch in plan.batches for item in batch.chunks]
        self.assertEqual([chunk.id for chunk in packed], [chunk.id for chunk in chunks])
        self.assertFalse(plan.oversized)
        self.assertGreater(len(plan.batches), 1)
        for batch in plan.batches:
            self.assertLessEqual(batch.chars, limit)
            self.assertEqual(batch.chars, len(batch.message))
            self.assertEqual(batch.message, render(batch.chunks))

    def test_plan_requests_reports_single_chunk_overflow(self) -> None:
        chunk = _chunk("too-big", "a.c", "y" * 50)
        plan = plan_requests([chunk], lambda batch: "".join(item.text for item in batch), 10)
        self.assertEqual(plan.batches, [])
        self.assertEqual([item.id for item in plan.oversized], ["too-big"])

    def test_plan_requests_respects_map_chunk_count_limit(self) -> None:
        chunks = [_chunk(f"C{i}", "tiny.c", "tiny") for i in range(25)]

        def render(batch: list[ContextChunk]) -> str:
            return "HDR" + "".join(item.text for item in batch)

        plan = plan_requests(chunks, render, 10_000, max_chunks=MAX_MAP_CHUNKS_PER_CALL)
        sizes = [len(batch.chunks) for batch in plan.batches]
        self.assertEqual(sizes, [8, 8, 8, 1])
        self.assertEqual(MAX_MAP_CHUNKS_PER_CALL, 8)
        packed = [item for batch in plan.batches for item in batch.chunks]
        self.assertEqual([chunk.id for chunk in packed], [chunk.id for chunk in chunks])
        self.assertFalse(plan.oversized)
        for batch in plan.batches:
            self.assertLessEqual(len(batch.chunks), MAX_MAP_CHUNKS_PER_CALL)
            self.assertLessEqual(batch.chars, 10_000)
            self.assertEqual(batch.message, render(batch.chunks))

    def test_plan_requests_character_limit_still_applies_with_fanout(self) -> None:
        chunks = [
            _chunk("C1", "a.c", "X" * 40),
            _chunk("C2", "b.c", "Y" * 40),
        ]

        def render(batch: list[ContextChunk]) -> str:
            return "".join(item.text for item in batch)

        limit = 50
        self.assertLess(2, MAX_MAP_CHUNKS_PER_CALL)
        self.assertGreater(len(render(chunks)), limit)
        self.assertLessEqual(len(render([chunks[0]])), limit)
        self.assertLessEqual(len(render([chunks[1]])), limit)
        plan = plan_requests(chunks, render, limit, max_chunks=MAX_MAP_CHUNKS_PER_CALL)
        self.assertEqual([len(batch.chunks) for batch in plan.batches], [1, 1])
        self.assertFalse(plan.oversized)
        for batch in plan.batches:
            self.assertLessEqual(batch.chars, limit)
            self.assertLessEqual(len(batch.chunks), MAX_MAP_CHUNKS_PER_CALL)


class SerializedRequestBudgetTests(unittest.TestCase):
    def _run(self, corpus: ReviewCorpus, recorder: _ReviewRecorder, **kwargs):
        defaults = dict(
            corpus=corpus,
            synthesis_prompt="synth",
            map_prompt="<!-- merge-warden-map -->",
            reduce_prompt="<!-- merge-warden-reduce -->",
            call_model=recorder,
            commentable_section="(none)\n",
            max_map_request_chars=80_000,
            max_reduce_request_chars=80_000,
            map_overhead_chars=100,
        )
        defaults.update(kwargs)
        return run_hierarchical_review(**defaults)

    def _validation_budget(
        self,
        corpus: ReviewCorpus,
        matching: list[ContextChunk],
        need: rp.ContextNeed,
        related: list[Finding],
    ) -> tuple[int, rp.RequestPlan]:
        map_one = max(
            len(format_map_user_message(corpus, [chunk]))
            for chunk in corpus.reviewable_chunks
        )
        val_one = max(
            len(format_validation_user_message(corpus, [need], [chunk], related))
            for chunk in matching
        )
        # Map ingest appends chunk:<id> evidence, which slightly grows later
        # validation prompts relative to a hand-built related finding.
        limit = max(map_one, val_one) + 256
        plan = plan_requests(
            matching,
            lambda batch: format_validation_user_message(corpus, [need], batch, related),
            limit,
        )
        return limit, plan

    def test_serialized_map_message_never_exceeds_budget(self) -> None:
        chunks = [
            _chunk(
                f"file:src/p{i}.c:1",
                f"src/very/long/path/name/module_{i}/file.c",
                f"int f{i}(void) {{ return {i}; }}\nUNIQUE_MAP_{i}\n" + ("A" * 120),
            )
            for i in range(6)
        ]
        index = "Changed files:\n" + "\n".join(
            f"{i}. src/very/long/path/name/module_{i}/file.c +10 -2"
            for i in range(40)
        ) + "\n"
        corpus = _synthetic_corpus(chunks, index=index)
        reviewable = corpus.reviewable_chunks
        single_max = max(len(format_map_user_message(corpus, [chunk])) for chunk in reviewable)
        combined = len(format_map_user_message(corpus, reviewable))
        self.assertGreater(combined, single_max)
        recorder = _ReviewRecorder()
        review, coverage, _store, _stats = self._run(
            corpus,
            recorder,
            max_map_request_chars=single_max,
            map_overhead_chars=24_000,
        )
        self.assertTrue(coverage.complete)
        self.assertTrue(recorder.map_messages)
        for message in recorder.map_messages:
            self.assertLessEqual(len(message), single_max)
        supplied = [chunk_id for message in recorder.map_messages for chunk_id in _chunk_ids_in_prompt(message)]
        self.assertEqual(set(supplied), {chunk.id for chunk in reviewable})
        self.assertEqual(len(supplied), len(reviewable))
        self.assertEqual(review["event"], "COMMENT")

    def test_wrong_overhead_estimate_still_enforces_serialized_budget(self) -> None:
        corpus = build_review_corpus(_inputs(diff="+ok\n+more\n", pr=_pr(body="small")))
        corpus.index = "Changed files:\n" + "\n".join(
            f"{i}. src/very/long/directory/name/file_{i:04d}.c +10 -2"
            for i in range(80)
        ) + "\n"
        reviewable = corpus.reviewable_chunks
        self.assertGreaterEqual(len(reviewable), 2)
        single_max = max(len(format_map_user_message(corpus, [chunk])) for chunk in reviewable)
        combined = len(format_map_user_message(corpus, reviewable))
        self.assertGreater(combined, single_max)
        recorder = _ReviewRecorder()
        review, coverage, _store, stats = self._run(
            corpus,
            recorder,
            max_map_request_chars=single_max,
            map_overhead_chars=1,
        )
        self.assertTrue(coverage.complete)
        self.assertTrue(recorder.map_messages)
        for message in recorder.map_messages:
            self.assertLessEqual(len(message), single_max)
        self.assertGreaterEqual(stats.map_calls, 2)
        self.assertEqual(review["event"], "COMMENT")

    def test_single_chunk_overflow_fails_closed_without_truncation(self) -> None:
        chunk = _chunk(
            "file:huge.c:1",
            "huge.c",
            "UNIQUE_CONTEXT_TAIL_999\n" + ("z" * 80),
        )
        corpus = _synthetic_corpus([chunk], index=("I" * 4000) + "\n", purpose="purpose\n")
        message = format_map_user_message(corpus, [chunk])
        limit = len(message) - 25
        self.assertGreater(limit, 0)
        self.assertGreater(len(message), limit)
        recorder = _ReviewRecorder()
        review, coverage, _store, stats = self._run(
            corpus,
            recorder,
            max_map_request_chars=limit,
            map_overhead_chars=1,
        )
        self.assertFalse(coverage.complete)
        self.assertIn(chunk.id, coverage.uncovered_chunk_ids)
        _assert_unsynthesized_fallback(self, review)
        self.assertEqual(recorder.map_messages, [])
        self.assertEqual(stats.map_calls, 0)
        self.assertEqual(stats.synthesis_calls, 0)
        self.assertTrue(any(chunk.id in note for note in stats.notes))
        self.assertNotIn("truncated", chunk.text)
        joined = "\n".join(recorder.map_messages)
        self.assertNotIn("UNIQUE_CONTEXT_TAIL_999", joined)

    def test_validation_uses_all_matching_chunks_across_batches(self) -> None:
        map_chunk = _chunk(
            "diff:src/foo.c:1",
            "src/foo.c",
            "+#include \"include/foo.h\"\nUNIQUE_MAP_TAIL\n",
            kind="diff",
        )
        matching = [
            _chunk(
                f"file:include/foo.h:{index}",
                "include/foo.h",
                f"/* part {index} */\nUNIQUE_CONTEXT_TAIL_{index}\n" + ("H" * 350),
            )
            for index in range(1, 5)
        ]
        matching[-1] = _chunk(
            "file:include/foo.h:4",
            "include/foo.h",
            "/* part 4 */\nUNIQUE_CONTEXT_TAIL_999\n" + ("H" * 350),
        )
        corpus = _synthetic_corpus([map_chunk], index="Changed files:\n- include/foo.h +4 -0\n")
        corpus.source_chunks = matching
        recorder = _ReviewRecorder(
            findings=[
                {
                    "id": "F17",
                    "severity": "BLOCKING",
                    "path": "src/foo.c",
                    "side": "RIGHT",
                    "line": 1,
                    "body": "Need include/foo.h ownership contract",
                    "confidence": "LIKELY",
                    "evidence": [],
                }
            ],
            needs_context=[{"path": "include/foo.h", "reason": "ownership contract"}],
            synthesis_event="REQUEST_CHANGES",
            synthesis_body="# REQUEST CHANGES\n",
        )
        related = [
            Finding(
                id="F17",
                severity="BLOCKING",
                path="src/foo.c",
                side="RIGHT",
                line=1,
                body="Need include/foo.h ownership contract",
                confidence="LIKELY",
                evidence=["chunk:diff:src/foo.c:1"],
            )
        ]
        need = rp.ContextNeed(path="include/foo.h", reason="ownership contract")
        limit, plan = self._validation_budget(corpus, matching, need, related)
        self.assertFalse(plan.oversized)
        self.assertGreater(len(plan.batches), 1)
        review, coverage, store, stats = self._run(
            corpus,
            recorder,
            max_map_request_chars=limit,
            map_overhead_chars=1,
        )
        self.assertTrue(coverage.complete)
        self.assertGreater(len(recorder.validation_messages), 1)
        seen = {
            chunk_id
            for message in recorder.validation_messages
            for chunk_id in _chunk_ids_in_prompt(message)
        }
        self.assertEqual(seen, {chunk.id for chunk in matching})
        for message in recorder.validation_messages:
            self.assertLessEqual(len(message), limit)
        self.assertIn("UNIQUE_CONTEXT_TAIL_999", "\n".join(recorder.validation_messages))
        self.assertIn("UNIQUE_CONTEXT_TAIL_999", matching[-1].text)
        self.assertEqual(stats.validation_chunks_acknowledged, len(matching))
        self.assertEqual(stats.validation_chunks_sent, len(matching))
        self.assertEqual(stats.validation_chunks, len(matching))
        self.assertEqual(store.findings[_fid("F17")].confidence, "LIKELY")
        self.assertEqual(review["event"], "REQUEST_CHANGES")

    def test_validation_call_cap_is_enforced(self) -> None:
        map_chunk = _chunk(
            "diff:src/foo.c:1",
            "src/foo.c",
            "+#include \"include/foo.h\"\n",
            kind="diff",
        )
        matching = [
            _chunk(
                f"file:include/foo.h:{index}",
                "include/foo.h",
                f"/* part {index} */\n" + ("V" * 400),
            )
            for index in range(1, 5)
        ]
        corpus = _synthetic_corpus([map_chunk])
        corpus.source_chunks = matching
        recorder = _ReviewRecorder(
            findings=[
                {
                    "id": "F17",
                    "severity": "MAJOR",
                    "path": "src/foo.c",
                    "body": "Need include/foo.h to decide correctness",
                    "confidence": "LIKELY",
                    "evidence": [],
                }
            ],
            needs_context=[{"path": "include/foo.h", "reason": "cross-context check"}],
        )
        related = [
            Finding(
                id="F17",
                severity="MAJOR",
                path="src/foo.c",
                side="RIGHT",
                line=1,
                body="Need include/foo.h to decide correctness",
                confidence="LIKELY",
                evidence=["chunk:diff:src/foo.c:1"],
            )
        ]
        need = rp.ContextNeed(path="include/foo.h", reason="cross-context check")
        limit, plan = self._validation_budget(corpus, matching, need, related)
        self.assertFalse(plan.oversized)
        self.assertGreater(len(plan.batches), 2)
        self.assertEqual(MAX_VALIDATION_CALLS, 8)
        with mock.patch.object(rp, "MAX_VALIDATION_CALLS", 2):
            review, coverage, store, stats = self._run(
                corpus,
                recorder,
                max_map_request_chars=limit,
                map_overhead_chars=1,
            )
        self.assertTrue(coverage.complete)
        self.assertEqual(stats.validation_calls, 2)
        self.assertLess(stats.validation_calls, len(matching))
        self.assertTrue(
            any("validation call limit reached" in note for note in stats.notes)
        )
        seen = {
            chunk_id
            for message in recorder.validation_messages
            for chunk_id in _chunk_ids_in_prompt(message)
        }
        self.assertLess(len(seen), len(matching))
        self.assertIn("validation:incomplete:include/foo.h", store.findings[_fid("F17")].evidence)
        self.assertEqual(store.findings[_fid("F17")].confidence, "LIKELY")
        self.assertEqual(review["event"], "COMMENT")
        self.assertIn("LIKELY", recorder.synthesis_messages[0])
        self.assertIn("validation:incomplete:include/foo.h", recorder.synthesis_messages[0])

    def test_validation_retry_messages_stay_within_budget(self) -> None:
        map_chunk = _chunk(
            "diff:src/foo.c:1",
            "src/foo.c",
            "+#include \"include/foo.h\"\nUNIQUE_MAP_TAIL\n",
            kind="diff",
        )
        matching = [
            _chunk(
                f"file:include/foo.h:{index}",
                "include/foo.h",
                f"/* part {index} */\nUNIQUE_CONTEXT_TAIL_{index}\n" + ("H" * 350),
            )
            for index in range(1, 5)
        ]
        corpus = _synthetic_corpus(
            [map_chunk],
            index="Changed files:\n- include/foo.h +4 -0\n",
        )
        corpus.source_chunks = matching
        related = [
            Finding(
                id="F17",
                severity="BLOCKING",
                path="src/foo.c",
                side="RIGHT",
                line=1,
                body="Need include/foo.h ownership contract",
                confidence="LIKELY",
                evidence=["chunk:diff:src/foo.c:1"],
            )
        ]
        need = rp.ContextNeed(path="include/foo.h", reason="ownership contract")
        limit, plan = self._validation_budget(corpus, matching, need, related)
        self.assertFalse(plan.oversized)
        self.assertGreater(len(plan.batches), 1)

        recorder = _ReviewRecorder(
            findings=[
                {
                    "id": "F17",
                    "severity": "BLOCKING",
                    "path": "src/foo.c",
                    "side": "RIGHT",
                    "line": 1,
                    "body": "Need include/foo.h ownership contract",
                    "confidence": "LIKELY",
                    "evidence": [],
                }
            ],
            needs_context=[{"path": "include/foo.h", "reason": "ownership contract"}],
        )
        state = {"n": 0}

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system and "Context requests" in user:
                ids = _chunk_ids_in_prompt(user)
                recorder.validation_messages.append(user)
                state["n"] += 1
                if state["n"] == 1:
                    return _map_chunks_json(ids[:1])
                return _map_chunks_json(ids)
            return recorder(system, user)

        review, coverage, store, stats = self._run(
            corpus,
            fake,
            max_map_request_chars=limit,
            map_overhead_chars=1,
        )
        self.assertTrue(coverage.complete)
        self.assertGreater(len(recorder.validation_messages), 1)
        self.assertGreaterEqual(state["n"], 2)
        for message in recorder.validation_messages:
            self.assertLessEqual(len(message), limit)
        self.assertEqual(stats.validation_chunks_acknowledged, len(matching))
        self.assertNotIn(INCOMPLETE_FOO, store.findings[_fid("F17")].evidence)
        self.assertEqual(review["event"], "COMMENT")

    def test_map_and_validation_keep_chunk_tails(self) -> None:
        chunks = [
            _chunk("file:a.c:1", "a.c", "head-a\nUNIQUE_CONTEXT_TAIL_A\n" + ("a" * 200)),
            _chunk("file:b.c:1", "b.c", "head-b\nUNIQUE_CONTEXT_TAIL_999\n" + ("b" * 200)),
            _chunk("file:c.h:1", "c.h", "head-c\nUNIQUE_CONTEXT_TAIL_C\n" + ("c" * 200)),
        ]
        corpus = _synthetic_corpus(chunks, index=("N" * 400) + "\n")
        single_max = max(len(format_map_user_message(corpus, [chunk])) for chunk in chunks)
        combined = len(format_map_user_message(corpus, chunks))
        self.assertGreater(combined, single_max)
        recorder = _ReviewRecorder(
            findings=[
                {
                    "id": "F1",
                    "severity": "MINOR",
                    "path": "a.c",
                    "body": "Check c.h for contract",
                    "confidence": "QUESTION",
                    "evidence": [],
                }
            ],
            needs_context=[{"path": "c.h", "reason": "contract"}],
        )
        _review, coverage, _store, _stats = self._run(
            corpus,
            recorder,
            max_map_request_chars=single_max,
            map_overhead_chars=1,
        )
        self.assertTrue(coverage.complete)
        dispatched = "\n".join(recorder.map_messages + recorder.validation_messages)
        self.assertIn("UNIQUE_CONTEXT_TAIL_999", dispatched)
        self.assertIn("UNIQUE_CONTEXT_TAIL_C", dispatched)
        self.assertNotIn("[truncated]", dispatched)
        for message in recorder.map_messages + recorder.validation_messages:
            self.assertLessEqual(len(message), single_max)

    def test_incomplete_coverage_behavior_unchanged_when_chunk_cannot_fit(self) -> None:
        corpus = build_review_corpus(_inputs(diff="+ok\n", pr=_pr(body="small")))
        first = corpus.reviewable_chunks[0]
        corpus.index = ("Q" * 8000) + "\n"
        message = format_map_user_message(corpus, [first])
        recorder = _ReviewRecorder()
        review, coverage, _store, _stats = self._run(
            corpus,
            recorder,
            max_map_request_chars=max(len(message) - 100, 50),
            map_overhead_chars=1,
        )
        self.assertFalse(coverage.complete)
        _assert_unsynthesized_fallback(self, review)
        self.assertIn("could not complete a full review", review["body"])


class MapRetryBudgetTests(unittest.TestCase):
    """Retries are bounded by the map budget, not by a fixed counter.

    Both regressions below are real production failures: a single chunk was
    abandoned while minutes of map budget were still unspent, which forced
    coverage incomplete and a COMMENT verdict. Each is paired with its
    inverse, where the budget really is gone and abandoning is correct, so
    "retry forever" cannot pass this suite.
    """

    def _clocked_run(self, corpus, fake, clock, deadline, **kwargs):
        slept: list[float] = []

        def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            clock["now"] += seconds

        with mock.patch.object(rp.time, "monotonic", lambda: clock["now"]):
            with mock.patch.object(rp.time, "sleep", fake_sleep):
                outcome = _run_hierarchical(corpus, fake, deadline=deadline, **kwargs)
        return outcome, slept

    @staticmethod
    def _separate_batch_chunks(count: int) -> list[ContextChunk]:
        """Chunks large enough that packing gives each its own map batch."""
        size = MAP_SOFT_REQUEST_TARGET_CHARS - 1_000
        return [
            _chunk(f"C{index}", f"c{index}.c", "x" * size)
            for index in range(1, count + 1)
        ]

    @staticmethod
    def _single_chunk_model(clock, attempts, failure, cost, fail_times):
        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                ids = _chunk_ids_in_prompt(user)
                attempts.append(rp.map_call_timeout_override.get())
                if len(attempts) <= fail_times:
                    clock["now"] += cost
                    raise failure()
                return _map_chunks_json(ids)
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        return fake

    def test_capacity_rejection_retries_while_the_budget_allows(self) -> None:
        """gombit run 32850191741: 503s abandoned with ~183s of map left."""
        corpus = _synthetic_corpus(_tiny_chunks(1))
        clock = {"now": 10_000.0}
        deadline = clock["now"] + 840.0
        attempts: list[float | None] = []
        fake = self._single_chunk_model(
            clock,
            attempts,
            lambda: ProviderRequestError(
                ProviderFailureKind.CAPACITY, "Gemini HTTP 503: high demand"
            ),
            cost=24.0,
            fail_times=4,
        )

        (_review, coverage, _store, stats), slept = self._clocked_run(
            corpus, fake, clock, deadline
        )
        self.assertTrue(coverage.complete)
        self.assertEqual(len(attempts), 5)
        self.assertEqual(stats.map_capacity_retries, 4)
        self.assertEqual(slept, [4.0, 8.0, 16.0, 30.0])

    def test_capacity_rejection_stops_when_the_budget_cannot_fund_a_retry(
        self,
    ) -> None:
        corpus = _synthetic_corpus(_tiny_chunks(1))
        clock = {"now": 10_000.0}
        deadline = clock["now"] + 840.0
        attempts: list[float | None] = []
        fake = self._single_chunk_model(
            clock,
            attempts,
            lambda: ProviderRequestError(
                ProviderFailureKind.CAPACITY, "Gemini HTTP 503: high demand"
            ),
            cost=150.0,
            fail_times=99,
        )

        (review, coverage, _store, stats), _slept = self._clocked_run(
            corpus, fake, clock, deadline
        )
        self.assertFalse(coverage.complete)
        self.assertEqual(review["event"], "COMMENT")
        # It must retry once (abandoning on the first 503 is the bug being
        # fixed) and then stop on budget, well short of the retry ceiling.
        self.assertEqual(len(attempts), 2)
        self.assertEqual(stats.map_capacity_retries, 1)
        self.assertLess(stats.map_capacity_retries, MAP_CAPACITY_RETRIES)
        self.assertTrue(
            any("uncovered after 1 capacity retry" in note for note in stats.notes),
            stats.notes,
        )

    def test_capacity_retry_is_refused_when_backoff_would_leave_less_than_a_call(
        self,
    ) -> None:
        """A Retry-After that would leave 149s must not be slept out.

        Planning and the sleep gate both size the leftover against
        ``map_call_seconds_needed``. Sleeping 4s and then skipping the retry
        would burn time validation could still use.
        """
        corpus = _synthetic_corpus(_tiny_chunks(1))
        clock = {"now": 10_000.0}
        deadline = clock["now"] + 840.0
        delay = MAP_CAPACITY_BACKOFF_SECONDS[0]
        leftover = rp.map_call_seconds_needed() - 1.0
        attempts: list[float | None] = []
        fake = self._single_chunk_model(
            clock,
            attempts,
            lambda: ProviderRequestError(
                ProviderFailureKind.CAPACITY,
                "Gemini HTTP 503: high demand",
                retry_after_seconds=delay,
            ),
            cost=420.0 - leftover - delay,
            fail_times=99,
        )

        (_review, coverage, _store, stats), slept = self._clocked_run(
            corpus, fake, clock, deadline
        )
        self.assertFalse(coverage.complete)
        self.assertEqual(attempts, [None])
        self.assertEqual(slept, [])
        self.assertEqual(stats.map_capacity_retries, 0)

    def test_capacity_retry_waits_when_the_dispatch_envelope_still_fits(
        self,
    ) -> None:
        """The inverse: leftover exactly equal to a call budget must still sleep."""
        corpus = _synthetic_corpus(_tiny_chunks(1))
        clock = {"now": 10_000.0}
        deadline = clock["now"] + 840.0
        delay = MAP_CAPACITY_BACKOFF_SECONDS[0]
        leftover = rp.map_call_seconds_needed()
        attempts: list[float | None] = []
        fake = self._single_chunk_model(
            clock,
            attempts,
            lambda: ProviderRequestError(
                ProviderFailureKind.CAPACITY,
                "Gemini HTTP 503: high demand",
                retry_after_seconds=delay,
            ),
            cost=420.0 - leftover - delay,
            fail_times=1,
        )

        (_review, coverage, _store, stats), slept = self._clocked_run(
            corpus, fake, clock, deadline
        )
        self.assertTrue(coverage.complete)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(slept, [delay])
        self.assertEqual(stats.map_capacity_retries, 1)

    def test_multi_chunk_capacity_failure_never_splits(self) -> None:
        """Splitting doubles the request rate against a load-shedding provider.

        It would also mint fresh retry signatures, resetting the per-request
        ceiling and letting one flapping batch sleep away the map stage.
        """
        corpus = _synthetic_corpus(_tiny_chunks(4))
        clock = {"now": 10_000.0}
        deadline = clock["now"] + 840.0
        shapes: list[int] = []

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                shapes.append(len(_chunk_ids_in_prompt(user)))
                raise ProviderRequestError(
                    ProviderFailureKind.CAPACITY, "Gemini HTTP 503: high demand"
                )
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        (review, coverage, _store, stats), slept = self._clocked_run(
            corpus, fake, clock, deadline
        )
        self.assertEqual(set(shapes), {4})
        self.assertEqual(stats.map_batches_split, 0)
        self.assertEqual(len(shapes), MAP_CAPACITY_RETRIES + 1)
        self.assertEqual(slept, list(MAP_CAPACITY_BACKOFF_SECONDS))
        self.assertLessEqual(sum(slept), rp.MAX_MAP_CAPACITY_SLEEP_SECONDS)
        self.assertFalse(coverage.complete)
        self.assertEqual(review["event"], "COMMENT")

    def test_independent_capacity_rejections_open_the_provider_circuit(self) -> None:
        """Four concurrent 503s are a provider outage, not four unlucky batches.

        The previous bound was the 120s stage-wide sleep cap, which still
        spent minutes retrying an unhealthy model. The circuit opens on the
        third independent capacity failure and does not dispatch retries.
        """
        corpus = _synthetic_corpus(self._separate_batch_chunks(4))
        clock = {"now": 10_000.0}
        deadline = clock["now"] + 840.0
        calls: list[tuple[str, ...]] = []

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                ids = tuple(_chunk_ids_in_prompt(user))
                calls.append(ids)
                raise ProviderRequestError(
                    ProviderFailureKind.CAPACITY,
                    "Gemini HTTP 503: high demand",
                    retry_after_seconds=rp.MAX_MAP_CAPACITY_BACKOFF_SECONDS,
                )
            raise AssertionError("circuit-open must not call later stages")

        (review, coverage, _store, stats), slept = self._clocked_run(
            corpus, fake, clock, deadline, map_concurrency=4
        )
        self.assertTrue(stats.provider_circuit_open)
        self.assertFalse(stats.map_deadline_exhausted)
        self.assertFalse(stats.deadline_exhausted)
        self.assertEqual(stats.map_capacity_retries, 0)
        self.assertEqual(stats.synthesis_calls, 0)
        self.assertEqual(len(calls), 4)
        self.assertEqual(slept, [])
        self.assertFalse(coverage.complete)
        self.assertEqual(review["event"], "COMMENT")
        self.assertEqual(review.get("comments") or [], [])
        self.assertIn("provider circuit open", stats.footer())
        self.assertNotIn("map budget exhausted", stats.footer())
        self.assertIn("provider circuit opened", " ".join(stats.notes).lower())
        _assert_unsynthesized_fallback(self, review)

    def test_two_independent_capacity_failures_still_retry(self) -> None:
        """The circuit needs three failures; two unlucky batches still back off."""
        corpus = _synthetic_corpus(self._separate_batch_chunks(2))
        clock = {"now": 10_000.0}
        deadline = clock["now"] + 840.0
        rejected: set[tuple[str, ...]] = set()

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                ids = tuple(_chunk_ids_in_prompt(user))
                if ids not in rejected:
                    rejected.add(ids)
                    raise ProviderRequestError(
                        ProviderFailureKind.CAPACITY,
                        "Gemini HTTP 503: high demand",
                        retry_after_seconds=rp.MAX_MAP_CAPACITY_BACKOFF_SECONDS,
                    )
                return _map_chunks_json(list(ids))
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        (review, coverage, _store, stats), slept = self._clocked_run(
            corpus, fake, clock, deadline, map_concurrency=2
        )
        self.assertFalse(stats.provider_circuit_open)
        self.assertEqual(stats.map_capacity_retries, 2)
        self.assertTrue(slept)
        self.assertLessEqual(sum(slept), rp.MAX_MAP_CAPACITY_SLEEP_SECONDS)
        self.assertTrue(coverage.complete)
        self.assertEqual(review["event"], "COMMENT")

    def test_widened_retry_queued_behind_work_rechecks_its_own_budget(
        self,
    ) -> None:
        """The dispatch gate must use the item's budget, not the constant.

        A widened retry is affordable when planned, then sibling batches spend
        the stage down while it waits in the queue. At dispatch there is still
        room for a standard call but not for the widened one, so gating on the
        constant would send it and let the deadline clamp it straight back to
        the clock that already timed out.
        """
        corpus = _synthetic_corpus(self._separate_batch_chunks(3))
        clock = {"now": 10_000.0}
        deadline = clock["now"] + 840.0
        map_cutoff = map_stage_deadline(deadline)
        dispatched: list[tuple[str, float | None, float]] = []

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                ids = _chunk_ids_in_prompt(user)
                dispatched.append(
                    (
                        ",".join(ids),
                        rp.map_call_timeout_override.get(),
                        map_cutoff - clock["now"],
                    )
                )
                if ids == ["C1"]:
                    clock["now"] += MAP_HTTP_TIMEOUT_SECONDS
                    raise ProviderRequestError(
                        ProviderFailureKind.LATENCY_TIMEOUT, "timed out"
                    )
                # Siblings spend the stage down while the retry is queued.
                clock["now"] += 40.0 if ids == ["C2"] else 60.0
                return _map_chunks_json(ids)
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        (review, coverage, _store, stats), _slept = self._clocked_run(
            corpus, fake, clock, deadline, map_concurrency=1
        )
        # Whatever else happened, no widened call ran without its own budget.
        for name, override, remaining in dispatched:
            if override is None:
                continue
            self.assertGreaterEqual(
                remaining,
                override + rp.MAP_CALL_BUDGET_MARGIN_SECONDS,
                f"{name} dispatched with {remaining}s for a {override}s clock",
            )
        self.assertIn("C1", coverage.uncovered_chunk_ids)
        self.assertFalse(coverage.complete)
        self.assertEqual(review["event"], "COMMENT")
        self.assertTrue(
            any("widened retry no longer fits" in note for note in stats.notes),
            stats.notes,
        )

    def test_unaffordable_widened_retry_is_refused_not_downgraded(self) -> None:
        """When the stage cannot fund the escalation, abandon the chunk.

        Re-sending under the clock that already timed out would spend the rest
        of the stage proving the same thing, so the chunk is left uncovered and
        the review fail-closes instead.
        """
        corpus = _synthetic_corpus(self._separate_batch_chunks(2))
        clock = {"now": 10_000.0}
        deadline = clock["now"] + 840.0
        map_cutoff = map_stage_deadline(deadline)
        dispatched: list[float | None] = []

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                ids = _chunk_ids_in_prompt(user)
                dispatched.append(rp.map_call_timeout_override.get())
                if ids == ["C1"]:
                    # Leave a standard call budget, less than a widened retry.
                    clock["now"] = map_cutoff - MAP_CALL_BUDGET_SECONDS
                    raise ProviderRequestError(
                        ProviderFailureKind.LATENCY_TIMEOUT, "timed out"
                    )
                return _map_chunks_json(ids)
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        (review, coverage, _store, stats), _slept = self._clocked_run(
            corpus, fake, clock, deadline, map_concurrency=1
        )
        # C1 was never re-sent, under any clock.
        self.assertEqual(dispatched.count(None), len(dispatched))
        self.assertEqual(stats.map_latency_retries, 0)
        self.assertIn("C1", coverage.uncovered_chunk_ids)
        self.assertFalse(coverage.complete)
        self.assertEqual(review["event"], "COMMENT")
        self.assertTrue(
            any("no budget for a longer retry" in note for note in stats.notes),
            stats.notes,
        )

    def test_deferred_batch_does_not_strand_sequential_ingest(self) -> None:
        """Out-of-order dispatch must not let `next_ingest` skip live work.

        Ingestion is strictly sequential, but capacity deferral dispatches out
        of sequence. If the scheduler advances past a sequence that is merely
        waiting out a backoff, that result lands in `completed` behind the
        cursor and is never read: the loop never drains and never exits. This
        hangs the whole action rather than fail-closing, so it is guarded with
        a bounded join instead of asserting on the result alone.
        """
        corpus = _synthetic_corpus(self._separate_batch_chunks(2))
        clock = {"now": 10_000.0}
        deadline = clock["now"] + 840.0
        rejected: set[tuple[str, ...]] = set()

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                ids = _chunk_ids_in_prompt(user)
                key = tuple(ids)
                if key not in rejected:
                    rejected.add(key)
                    # Different Retry-After values make the second batch
                    # dispatch and finish while the first is still deferred.
                    raise ProviderRequestError(
                        ProviderFailureKind.CAPACITY,
                        "Gemini HTTP 503: high demand",
                        retry_after_seconds=40.0 if ids == ["C1"] else 5.0,
                    )
                return _map_chunks_json(ids)
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        box: dict[str, object] = {}
        finished = threading.Event()

        def run() -> None:
            try:
                box["outcome"], _slept = self._clocked_run(
                    corpus, fake, clock, deadline, map_concurrency=2
                )
            finally:
                finished.set()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.assertTrue(
            finished.wait(30.0), "map scheduler did not terminate"
        )
        _review, coverage, _store, _stats = box["outcome"]
        self.assertTrue(coverage.complete)

    def test_scheduler_ingests_finished_work_before_sleeping(self) -> None:
        """A backoff must not stall results that are already back.

        C1 is deferred for capacity while C2's malformed response sits in the
        completed queue. Ingesting C2 releases its retry immediately, so the
        scheduler must drain what it already has before sleeping out C1's
        backoff. Sleeping first idles every worker for the whole delay.
        """
        corpus = _synthetic_corpus(self._separate_batch_chunks(2))
        clock = {"now": 10_000.0}
        deadline = clock["now"] + 840.0
        events: list[str] = []
        rejected: set[str] = set()

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                ids = _chunk_ids_in_prompt(user)
                events.append(f"call:{','.join(ids)}")
                if ids == ["C1"] and "C1" not in rejected:
                    rejected.add("C1")
                    raise ProviderRequestError(
                        ProviderFailureKind.CAPACITY,
                        "Gemini HTTP 503: high demand",
                        retry_after_seconds=rp.MAX_MAP_CAPACITY_BACKOFF_SECONDS,
                    )
                if ids == ["C2"] and "C2" not in rejected:
                    rejected.add("C2")
                    return "definitely not JSON {"
                return _map_chunks_json(ids)
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        def fake_sleep(seconds: float) -> None:
            events.append(f"sleep:{seconds}")
            clock["now"] += seconds

        with mock.patch.object(rp.time, "monotonic", lambda: clock["now"]):
            with mock.patch.object(rp.time, "sleep", fake_sleep):
                _review, coverage, _store, _stats = _run_hierarchical(
                    corpus, fake, deadline=deadline, map_concurrency=1
                )
        sleeps = [index for index, event in enumerate(events) if event.startswith("sleep:")]
        self.assertTrue(sleeps, events)
        # C2's retry was dispatchable the moment its result was ingested, so it
        # must not wait behind C1's backoff.
        self.assertEqual(events.count("call:C2"), 2, events)
        second_c2 = [
            index for index, event in enumerate(events) if event == "call:C2"
        ][1]
        self.assertLess(second_c2, sleeps[0], events)
        self.assertTrue(coverage.complete)

    def test_single_chunk_latency_timeout_is_retried_with_a_longer_clock(
        self,
    ) -> None:
        """brainrot run 32837289271: one timeout, zero retries, ~228s left."""
        corpus = _synthetic_corpus(_tiny_chunks(1))
        clock = {"now": 10_000.0}
        deadline = clock["now"] + 840.0
        attempts: list[float | None] = []
        fake = self._single_chunk_model(
            clock,
            attempts,
            lambda: ProviderRequestError(
                ProviderFailureKind.LATENCY_TIMEOUT,
                "xAI request timed out after 1 attempts",
            ),
            cost=MAP_HTTP_TIMEOUT_SECONDS,
            fail_times=1,
        )

        map_cutoff = map_stage_deadline(deadline)
        budget_at_dispatch: list[tuple[float | None, float]] = []

        def recording(system: str, user: str) -> str:
            budget_at_dispatch.append(
                (rp.map_call_timeout_override.get(), map_cutoff - clock["now"])
            )
            return fake(system, user)

        (_review, coverage, _store, stats), _slept = self._clocked_run(
            corpus, recording, clock, deadline
        )
        self.assertTrue(coverage.complete)
        self.assertEqual(stats.map_latency_retries, 1)
        # The retry runs under a widened clock; the first attempt does not.
        self.assertEqual(attempts, [None, MAP_HTTP_TIMEOUT_SECONDS * 1.5])
        widened = [
            (override, remaining)
            for override, remaining in budget_at_dispatch
            if override is not None
        ]
        self.assertEqual(len(widened), 1)
        # It was dispatched with room for the whole widened call, not clamped
        # back to the clock that already timed out.
        override, remaining = widened[0]
        self.assertGreaterEqual(
            remaining, override + rp.MAP_CALL_BUDGET_MARGIN_SECONDS
        )

    def test_single_chunk_latency_timeout_stops_without_budget_to_widen(
        self,
    ) -> None:
        corpus = _synthetic_corpus(_tiny_chunks(1))
        clock = {"now": 10_000.0}
        deadline = clock["now"] + 840.0
        attempts: list[float | None] = []
        fake = self._single_chunk_model(
            clock,
            attempts,
            lambda: ProviderRequestError(
                ProviderFailureKind.LATENCY_TIMEOUT,
                "xAI request timed out after 1 attempts",
            ),
            # Leaves 120s of the 420s map stage: not enough for a longer retry.
            cost=300.0,
            fail_times=99,
        )

        (review, coverage, _store, stats), _slept = self._clocked_run(
            corpus, fake, clock, deadline
        )
        self.assertFalse(coverage.complete)
        self.assertEqual(review["event"], "COMMENT")
        self.assertEqual(attempts, [None])
        self.assertEqual(stats.map_latency_retries, 0)
        self.assertTrue(
            any("no budget for a longer retry" in note for note in stats.notes),
            stats.notes,
        )

    def test_ceilings_still_bind_when_the_budget_is_unbounded(self) -> None:
        """Without a deadline, only the ceilings stop an always-failing chunk."""
        corpus = _synthetic_corpus(_tiny_chunks(1))
        clock = {"now": 0.0}
        attempts: list[float | None] = []
        fake = self._single_chunk_model(
            clock,
            attempts,
            lambda: ProviderRequestError(
                ProviderFailureKind.CAPACITY, "Gemini HTTP 503: high demand"
            ),
            cost=0.0,
            fail_times=99,
        )

        (_review, coverage, _store, stats), _slept = self._clocked_run(
            corpus, fake, clock, None
        )
        self.assertFalse(coverage.complete)
        self.assertLess(stats.map_attempts, MAX_MAP_ATTEMPTS)
        # Capacity is terminal once its ladder is spent: it does not fall
        # through to the single-chunk floor for a second round of retries.
        self.assertEqual(len(attempts), MAP_CAPACITY_RETRIES + 1)

    def test_retry_budget_refuses_what_it_cannot_afford(self) -> None:
        unlimited = rp.RetryBudget(remaining_seconds=None, attempts_left=1)
        self.assertTrue(unlimited.can_fund(10_000.0))
        no_attempts = rp.RetryBudget(remaining_seconds=10_000.0, attempts_left=0)
        self.assertFalse(no_attempts.can_fund())
        standard = rp.map_call_seconds_needed()
        exact = rp.RetryBudget(remaining_seconds=standard, attempts_left=5)
        self.assertTrue(exact.can_fund())
        self.assertFalse(
            rp.RetryBudget(
                remaining_seconds=standard - 0.5, attempts_left=5
            ).can_fund()
        )
        # Backoff is paid before the call, so it is part of what must fit.
        self.assertFalse(exact.can_fund(delay_seconds=1.0))
        self.assertTrue(
            rp.RetryBudget(
                remaining_seconds=standard + 30.0, attempts_left=5
            ).can_fund(delay_seconds=30.0)
        )

    def test_planner_and_dispatcher_ask_one_budget_question(self) -> None:
        """Planning must never fund a retry the dispatch gate would refuse.

        ``map_call_seconds_needed`` is the dispatch contract. Pinning
        ``can_fund`` to it exactly means a second, more optimistic margin
        cannot be reintroduced on the planning side without failing here.
        """
        for timeout in (
            None,
            MAP_HTTP_TIMEOUT_SECONDS,
            MAP_HTTP_TIMEOUT_SECONDS * 1.5,
            rp.MAP_MAX_HTTP_TIMEOUT_SECONDS,
        ):
            needed = rp.map_call_seconds_needed(timeout)
            with self.subTest(timeout=timeout):
                self.assertTrue(
                    rp.RetryBudget(
                        remaining_seconds=needed, attempts_left=4
                    ).can_fund(timeout)
                )
                self.assertFalse(
                    rp.RetryBudget(
                        remaining_seconds=needed - 0.5, attempts_left=4
                    ).can_fund(timeout)
                )

    def test_widened_retry_is_refused_inside_the_old_planning_gap(self) -> None:
        """A 210s retry planned at 217s remaining was skipped at dispatch.

        The planner allowed ``escalated + 5`` while dispatch demanded
        ``escalated + 10``, so this window planned, logged and enqueued a retry
        that could never run. One funding formula closes it.
        """
        corpus = _synthetic_corpus(_tiny_chunks(1))
        clock = {"now": 10_000.0}
        # 357s of map stage; one 140s timeout leaves exactly 217s.
        deadline = clock["now"] + 777.0
        attempts: list[float | None] = []
        fake = self._single_chunk_model(
            clock,
            attempts,
            lambda: ProviderRequestError(
                ProviderFailureKind.LATENCY_TIMEOUT,
                "xAI request timed out after 1 attempts",
            ),
            cost=MAP_HTTP_TIMEOUT_SECONDS,
            fail_times=1,
        )

        (_review, coverage, _store, stats), _slept = self._clocked_run(
            corpus, fake, clock, deadline
        )
        self.assertFalse(coverage.complete)
        self.assertEqual(attempts, [None])
        self.assertEqual(stats.map_latency_retries, 0)
        notes = " | ".join(stats.notes)
        self.assertIn("no budget for a longer retry", notes)
        # The planner must not announce a retry it cannot fund, and no batch
        # may be enqueued only for the dispatch gate to skip it.
        self.assertNotIn("retrying with a", notes)
        self.assertNotIn("no longer fits the map budget", notes)

    def test_widened_retry_runs_when_it_exactly_fits(self) -> None:
        """The inverse: at exactly the dispatch cost the retry must still run.

        Without this, refusing every widened retry would pass the test above.
        """
        corpus = _synthetic_corpus(_tiny_chunks(1))
        clock = {"now": 10_000.0}
        # 360s of map stage; one 140s timeout leaves exactly the 220s a 210s
        # widened call costs to dispatch.
        deadline = clock["now"] + 780.0
        attempts: list[float | None] = []
        fake = self._single_chunk_model(
            clock,
            attempts,
            lambda: ProviderRequestError(
                ProviderFailureKind.LATENCY_TIMEOUT,
                "xAI request timed out after 1 attempts",
            ),
            cost=MAP_HTTP_TIMEOUT_SECONDS,
            fail_times=1,
        )

        (_review, coverage, _store, stats), _slept = self._clocked_run(
            corpus, fake, clock, deadline
        )
        self.assertTrue(coverage.complete)
        self.assertEqual(attempts, [None, MAP_HTTP_TIMEOUT_SECONDS * 1.5])
        self.assertEqual(stats.map_latency_retries, 1)

    def test_transport_split_children_inherit_the_parent_retry_spend(
        self,
    ) -> None:
        """Per-chunk keying: a split must not mint a fresh retry ladder.

        Transport is the only path that both retries the same shape and then
        splits, so it is the only one that can catch a regression to keying on
        the request signature.
        """
        corpus = _synthetic_corpus(_tiny_chunks(2))
        shapes: list[int] = []

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                shapes.append(len(_chunk_ids_in_prompt(user)))
                raise ProviderRequestError(
                    ProviderFailureKind.TRANSIENT_TRANSPORT,
                    "connection reset after 0.5s",
                )
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        _review, coverage, _store, stats = _run_hierarchical(corpus, fake)
        # The parent spends the whole ladder, then splits once. Each child
        # inherits that spend, so neither gets retries of its own. Keyed on
        # the request signature instead, each child would start at zero.
        self.assertEqual(shapes, [2] * (MAP_TRANSPORT_RETRIES + 1) + [1, 1])
        self.assertEqual(stats.map_batches_split, 1)
        self.assertEqual(stats.map_attempts, MAP_TRANSPORT_RETRIES + 3)
        self.assertFalse(coverage.complete)

    def test_capacity_backoff_prefers_retry_after_and_clamps_it(self) -> None:
        self.assertEqual(rp.capacity_backoff_seconds(0, 12.0), 12.0)
        self.assertEqual(
            rp.capacity_backoff_seconds(0, 5_000.0),
            rp.MAX_MAP_CAPACITY_BACKOFF_SECONDS,
        )
        self.assertEqual(
            rp.capacity_backoff_seconds(0, None), MAP_CAPACITY_BACKOFF_SECONDS[0]
        )
        self.assertEqual(
            rp.capacity_backoff_seconds(99, None), MAP_CAPACITY_BACKOFF_SECONDS[-1]
        )
        # A zero or negative Retry-After must not defeat the backoff.
        self.assertEqual(
            rp.capacity_backoff_seconds(0, 0.0), MAP_CAPACITY_BACKOFF_SECONDS[0]
        )

    def test_escalated_timeout_grows_but_stays_capped(self) -> None:
        budget = rp.RetryBudget(remaining_seconds=10_000.0, attempts_left=4)
        self.assertEqual(
            rp.escalated_map_timeout(MAP_HTTP_TIMEOUT_SECONDS, budget),
            MAP_HTTP_TIMEOUT_SECONDS * 1.5,
        )
        self.assertEqual(
            rp.escalated_map_timeout(150.0, budget),
            min(150.0 * 1.5, rp.MAP_MAX_HTTP_TIMEOUT_SECONDS),
        )
        starved = rp.RetryBudget(remaining_seconds=30.0, attempts_left=4)
        self.assertIsNone(rp.escalated_map_timeout(MAP_HTTP_TIMEOUT_SECONDS, starved))
        # At the cap there is nothing left to grant, so do not repeat the same
        # doomed call under a new batch tag.
        self.assertIsNone(
            rp.escalated_map_timeout(rp.MAP_MAX_HTTP_TIMEOUT_SECONDS, budget)
        )
        self.assertIsNone(rp.escalated_map_timeout(10_000.0, budget))


def _tiny_chunks(count: int, prefix: str = "C") -> list[ContextChunk]:
    width = len(str(count))
    return [
        _chunk(
            f"{prefix}{index}",
            f"{prefix.lower()}{index}.c",
            f"tiny-{index:0{width}d}",
        )
        for index in range(1, count + 1)
    ]


def _heading_doc(title: str, sections: int, filler: str = "body") -> str:
    parts = [
        f"# {title} {index}\n{filler} {index}\n" for index in range(1, sections + 1)
    ]
    return "\n".join(parts)


class ProviderHealthTests(unittest.TestCase):
    """Circuit policy, independent of the map scheduler."""

    def test_isolated_failure_does_not_open(self) -> None:
        health = ProviderHealth()
        self.assertFalse(
            health.observe_capacity_failure("A", http_status=503)
        )
        self.assertFalse(health.circuit_open)
        health.observe_success()
        self.assertEqual(health.consecutive_failures, 0)

    def test_three_retries_of_one_request_do_not_open(self) -> None:
        health = ProviderHealth()
        for _ in range(5):
            self.assertFalse(
                health.observe_capacity_failure("batch-1", http_status=503)
            )
        self.assertFalse(health.circuit_open)
        self.assertEqual(health.distinct_requests(), 1)

    def test_independent_failures_open_at_threshold(self) -> None:
        health = ProviderHealth(provider="google", model="gemini-3.1-pro-preview")
        self.assertFalse(health.observe_capacity_failure("A", http_status=503))
        self.assertFalse(health.observe_capacity_failure("B", http_status=503))
        self.assertTrue(health.observe_capacity_failure("C", http_status=503))
        self.assertTrue(health.circuit_open)
        self.assertIn("3 availability failures", health.circuit_reason)
        self.assertEqual(health.label(), "google/gemini-3.1-pro-preview")
        self.assertGreaterEqual(
            health.consecutive_failures, PROVIDER_CIRCUIT_FAILURE_THRESHOLD
        )
        self.assertGreaterEqual(
            health.distinct_requests(), PROVIDER_CIRCUIT_MIN_INDEPENDENT_REQUESTS
        )

    def test_success_resets_streak(self) -> None:
        health = ProviderHealth()
        health.observe_capacity_failure("A", http_status=503)
        health.observe_capacity_failure("B", http_status=503)
        health.observe_success()
        self.assertFalse(health.observe_capacity_failure("C", http_status=503))
        self.assertFalse(health.circuit_open)
        self.assertEqual(health.consecutive_failures, 1)

    def test_success_after_open_does_not_close(self) -> None:
        health = ProviderHealth()
        health.observe_capacity_failure("A", http_status=503)
        health.observe_capacity_failure("B", http_status=503)
        health.observe_capacity_failure("C", http_status=503)
        health.observe_success()
        self.assertTrue(health.circuit_open)

    def test_retry_after_exceeding_remaining_budget_opens(self) -> None:
        health = ProviderHealth()
        opened = health.observe_capacity_failure(
            "A",
            http_status=429,
            retry_after_seconds=500.0,
            remaining_stage_seconds=120.0,
        )
        self.assertTrue(opened)
        self.assertIn("Retry-After", health.circuit_reason)

    def test_useful_retry_after_does_not_open_alone(self) -> None:
        health = ProviderHealth()
        opened = health.observe_capacity_failure(
            "A",
            http_status=429,
            retry_after_seconds=12.0,
            remaining_stage_seconds=400.0,
        )
        self.assertFalse(opened)

    def test_status_line_distinguishes_independent_failures(self) -> None:
        health = ProviderHealth()
        health.observe_capacity_failure("A", http_status=503)
        self.assertEqual(
            health.status_line(), "Provider health: 1 recent availability failure"
        )
        health.observe_capacity_failure("B", http_status=503)
        self.assertIn("2 failures across 2 independent requests", health.status_line())
        health.observe_capacity_failure("C", http_status=503)
        self.assertIn("3 failures across 3 independent requests", health.status_line())

    def test_identical_failures_are_summarized(self) -> None:
        health = ProviderHealth()
        self.assertFalse(health.should_summarize_failure("HTTP 503: high demand"))
        self.assertTrue(health.should_summarize_failure("HTTP 503: high demand"))
        self.assertFalse(health.should_summarize_failure("HTTP 429: slow down"))


class ProviderCircuitPipelineTests(unittest.TestCase):
    """Map scheduler honors the provider circuit without burning the stage."""

    def _capacity_error(self, status: int = 503, retry_after: float | None = None):
        return ProviderRequestError(
            ProviderFailureKind.CAPACITY,
            f"Gemini HTTP {status}: high demand",
            retry_after_seconds=retry_after,
            http_status=status,
        )

    def _always_capacity(self, status: int = 503):
        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                raise self._capacity_error(status)
            raise AssertionError(f"later stage {stage} must not run after circuit open")

        return fake

    def test_isolated_503_retries_and_succeeds(self) -> None:
        corpus = _synthetic_corpus(_tiny_chunks(1))
        clock = {"now": 10_000.0}
        calls = {"n": 0}

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                calls["n"] += 1
                if calls["n"] == 1:
                    raise self._capacity_error()
                return _map_chunks_json(_chunk_ids_in_prompt(user))
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        def fake_sleep(seconds: float) -> None:
            clock["now"] += seconds

        with mock.patch.object(rp.time, "monotonic", lambda: clock["now"]):
            with mock.patch.object(rp.time, "sleep", fake_sleep):
                review, coverage, _store, stats = _run_hierarchical(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertFalse(stats.provider_circuit_open)
        self.assertEqual(stats.map_capacity_retries, 1)
        self.assertEqual(review["event"], "COMMENT")

    def test_independent_503s_open_circuit_without_retries(self) -> None:
        corpus = _synthetic_corpus(MapRetryBudgetTests._separate_batch_chunks(4))
        calls: list[str] = []

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                calls.append(",".join(_chunk_ids_in_prompt(user)))
                raise self._capacity_error()
            raise AssertionError("synthesis must not run against an open circuit")

        review, coverage, _store, stats = _run_hierarchical(
            corpus, fake, map_concurrency=4
        )
        self.assertTrue(stats.provider_circuit_open)
        self.assertFalse(stats.map_deadline_exhausted)
        self.assertFalse(stats.deadline_exhausted)
        self.assertEqual(stats.map_capacity_retries, 0)
        self.assertEqual(stats.synthesis_calls, 0)
        self.assertEqual(len(calls), 4)
        self.assertFalse(coverage.complete)
        self.assertEqual(review["event"], "COMMENT")
        self.assertEqual(review.get("comments") or [], [])
        self.assertNotIn("# APPROVE", review["body"])
        self.assertIn("provider circuit open", stats.footer())
        _assert_unsynthesized_fallback(self, review)

    def test_no_new_primary_batch_after_circuit_opens(self) -> None:
        corpus = _synthetic_corpus(MapRetryBudgetTests._separate_batch_chunks(5))
        calls: list[str] = []

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                calls.append(",".join(_chunk_ids_in_prompt(user)))
                raise self._capacity_error()
            raise AssertionError("later stages must not run")

        _review, _coverage, _store, stats = _run_hierarchical(
            corpus, fake, map_concurrency=4
        )
        self.assertTrue(stats.provider_circuit_open)
        self.assertEqual(len(calls), 4)
        self.assertGreater(stats.provider_circuit_prevented_calls, 0)

    def test_in_flight_success_is_ingested_after_circuit_opens(self) -> None:
        corpus = _synthetic_corpus(MapRetryBudgetTests._separate_batch_chunks(4))
        started = threading.Barrier(4, timeout=5)
        release_success = threading.Event()
        real_observe = rp.ProviderHealth.observe_capacity_failure

        def observe_and_release(self, *args, **kwargs):
            opened = real_observe(self, *args, **kwargs)
            if self.circuit_open:
                release_success.set()
            return opened

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                ids = _chunk_ids_in_prompt(user)
                started.wait()
                if ids == ["C1"]:
                    if not release_success.wait(timeout=5):
                        raise AssertionError("success batch was not released")
                    return _map_chunks_json(ids)
                raise self._capacity_error()
            raise AssertionError("later stages must not run")

        with mock.patch.object(
            rp.ProviderHealth, "observe_capacity_failure", observe_and_release
        ):
            review, coverage, _store, stats = _run_hierarchical(
                corpus, fake, map_concurrency=4
            )
        self.assertTrue(stats.provider_circuit_open)
        self.assertNotIn("C1", coverage.uncovered_chunk_ids)
        self.assertGreaterEqual(stats.map_chunks_acknowledged, 1)
        self.assertEqual(stats.synthesis_calls, 0)
        self.assertEqual(review["event"], "COMMENT")

    def test_success_between_failures_does_not_open(self) -> None:
        corpus = _synthetic_corpus(MapRetryBudgetTests._separate_batch_chunks(3))
        attempts: dict[str, int] = {}

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                ids = _chunk_ids_in_prompt(user)
                lead = ids[0]
                attempts[lead] = attempts.get(lead, 0) + 1
                if lead == "C2" or attempts[lead] > 1:
                    return _map_chunks_json(ids)
                raise self._capacity_error()
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        def fake_sleep(seconds: float) -> None:
            return None

        with mock.patch.object(rp.time, "sleep", fake_sleep):
            _review, coverage, _store, stats = _run_hierarchical(
                corpus, fake, map_concurrency=1
            )
        self.assertFalse(stats.provider_circuit_open)
        self.assertNotIn("C2", coverage.uncovered_chunk_ids)

    def test_repeated_429s_open_circuit(self) -> None:
        corpus = _synthetic_corpus(MapRetryBudgetTests._separate_batch_chunks(3))

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                raise self._capacity_error(429, retry_after=4.0)
            raise AssertionError("later stages must not run")

        review, _coverage, _store, stats = _run_hierarchical(
            corpus, fake, map_concurrency=3
        )
        self.assertTrue(stats.provider_circuit_open)
        self.assertEqual(stats.synthesis_calls, 0)
        self.assertEqual(review["event"], "COMMENT")

    def test_single_429_with_useful_retry_after_retries(self) -> None:
        corpus = _synthetic_corpus(_tiny_chunks(1))
        clock = {"now": 10_000.0}
        calls = {"n": 0}

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                calls["n"] += 1
                if calls["n"] == 1:
                    raise self._capacity_error(429, retry_after=4.0)
                return _map_chunks_json(_chunk_ids_in_prompt(user))
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        def fake_sleep(seconds: float) -> None:
            clock["now"] += seconds

        with mock.patch.object(rp.time, "monotonic", lambda: clock["now"]):
            with mock.patch.object(rp.time, "sleep", fake_sleep):
                _review, coverage, _store, stats = _run_hierarchical(
                    corpus, fake, deadline=clock["now"] + 840.0
                )
        self.assertTrue(coverage.complete)
        self.assertFalse(stats.provider_circuit_open)
        self.assertEqual(stats.map_capacity_retries, 1)

    def test_retry_after_larger_than_remaining_budget_opens_circuit(self) -> None:
        corpus = _synthetic_corpus(_tiny_chunks(1))
        clock = {"now": 10_000.0}
        deadline = clock["now"] + 840.0
        map_cutoff = map_stage_deadline(deadline)

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                clock["now"] = map_cutoff - 80.0
                raise self._capacity_error(429, retry_after=500.0)
            raise AssertionError("later stages must not run")

        with mock.patch.object(rp.time, "monotonic", lambda: clock["now"]):
            review, coverage, _store, stats = _run_hierarchical(
                corpus, fake, deadline=deadline
            )
        self.assertTrue(stats.provider_circuit_open)
        self.assertFalse(stats.map_deadline_exhausted)
        self.assertFalse(coverage.complete)
        self.assertEqual(stats.synthesis_calls, 0)
        self.assertEqual(review["event"], "COMMENT")

    def test_latency_timeouts_do_not_open_circuit(self) -> None:
        corpus = _synthetic_corpus(MapRetryBudgetTests._separate_batch_chunks(3))

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                raise ProviderRequestError(
                    ProviderFailureKind.LATENCY_TIMEOUT, "timed out"
                )
            if stage in {"pre-reduce", "reduce"}:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        _review, coverage, _store, stats = _run_hierarchical(
            corpus, fake, map_concurrency=3
        )
        self.assertFalse(stats.provider_circuit_open)
        self.assertFalse(coverage.complete)

    def test_circuit_open_cannot_approve(self) -> None:
        corpus = _synthetic_corpus(MapRetryBudgetTests._separate_batch_chunks(3))
        review, coverage, _store, stats = _run_hierarchical(
            corpus, self._always_capacity(), map_concurrency=3
        )
        self.assertTrue(stats.provider_circuit_open)
        self.assertEqual(review["event"], "COMMENT")
        self.assertNotIn("# APPROVE", review["body"])
        self.assertFalse(coverage.complete)

    def test_unsynthesized_candidates_are_not_posted(self) -> None:
        corpus = _synthetic_corpus(MapRetryBudgetTests._separate_batch_chunks(4))
        success_ids = {"C1"}

        def fake(system: str, user: str) -> str:
            stage = mw.provider_call_stage(system, user)
            if stage == "map":
                ids = _chunk_ids_in_prompt(user)
                if set(ids) == success_ids:
                    return _map_chunks_json(
                        ids,
                        findings=[
                            {
                                "id": "F_LEAK",
                                "severity": "BLOCKING",
                                "path": "c1.c",
                                "side": "RIGHT",
                                "line": 1,
                                "body": "raw mapper candidate must not post",
                                "confidence": "CONFIRMED",
                            }
                        ],
                    )
                raise self._capacity_error()
            raise AssertionError("later stages must not run")

        review, _coverage, store, stats = _run_hierarchical(
            corpus, fake, map_concurrency=4
        )
        self.assertTrue(stats.provider_circuit_open)
        self.assertEqual(stats.synthesis_calls, 0)
        _assert_unsynthesized_fallback(self, review)
        self.assertTrue(any("raw mapper candidate" in item.body for item in store.findings.values()))
        self.assertNotIn("raw mapper candidate must not post", review["body"])

    def test_circuit_does_not_consume_map_stage_reserve_flag(self) -> None:
        corpus = _synthetic_corpus(MapRetryBudgetTests._separate_batch_chunks(3))
        _review, _coverage, _store, stats = _run_hierarchical(
            corpus, self._always_capacity(), map_concurrency=3
        )
        self.assertTrue(stats.provider_circuit_open)
        self.assertFalse(stats.map_deadline_exhausted)
        self.assertFalse(stats.deadline_exhausted)
        self.assertFalse(stats.validation_deadline_exhausted)
        self.assertFalse(stats.reduce_deadline_exhausted)


class MapFanoutAndDegradationTests(unittest.TestCase):
    def _run(self, corpus: ReviewCorpus, fake, **kwargs):
        return _run_hierarchical(corpus, fake, **kwargs)

    def _ack_model(self, handler):
        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system:
                if "Context requests" in user:
                    return _map_chunks_json(_chunk_ids_in_prompt(user))
                return handler(user)
            if "merge-warden-reduce" in system:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        return fake

    def test_non_json_map_response_is_retried(self) -> None:
        corpus = _synthetic_corpus(_tiny_chunks(1))
        state = {"n": 0}

        def handler(user: str) -> str:
            state["n"] += 1
            if state["n"] == 1:
                return "not json"
            return _map_chunks_json(_chunk_ids_in_prompt(user))

        review, coverage, _store, stats = self._run(corpus, self._ack_model(handler))
        self.assertTrue(coverage.complete)
        self.assertEqual(coverage.uncovered_chunk_ids, [])
        self.assertGreaterEqual(stats.map_attempts, 2)
        self.assertEqual(stats.map_non_json_responses, 1)
        self.assertEqual(stats.map_calls_succeeded, 1)
        self.assertEqual(review["event"], "COMMENT")

    def test_non_json_multi_chunk_response_degrades_batch(self) -> None:
        chunks = _tiny_chunks(4)
        corpus = _synthetic_corpus(chunks)
        seen_sizes: list[int] = []

        def handler(user: str) -> str:
            ids = _chunk_ids_in_prompt(user)
            seen_sizes.append(len(ids))
            if len(ids) >= 4:
                return "definitely not JSON {"
            return _map_chunks_json(ids)

        _review, coverage, _store, stats = self._run(corpus, self._ack_model(handler))
        self.assertTrue(coverage.complete)
        self.assertIn(4, seen_sizes)
        self.assertTrue(any(size == 2 for size in seen_sizes))
        self.assertGreaterEqual(stats.map_batches_split, 1)
        self.assertGreaterEqual(stats.map_non_json_responses, 1)
        self.assertEqual({chunk.id for chunk in chunks} - set(coverage.uncovered_chunk_ids), {chunk.id for chunk in chunks})

    def test_provider_exception_degrades_batch(self) -> None:
        chunks = _tiny_chunks(4)
        corpus = _synthetic_corpus(chunks)
        calls = {"n": 0}

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system and "Context requests" not in user:
                ids = _chunk_ids_in_prompt(user)
                calls["n"] += 1
                if len(ids) >= 4:
                    raise RuntimeError("provider unavailable")
                return _map_chunks_json(ids)
            if "merge-warden-reduce" in system:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps({"event": "COMMENT", "body": "# COMMENT\n", "comments": []})

        _review, coverage, _store, stats = self._run(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertGreaterEqual(stats.map_provider_failures, 1)
        self.assertEqual(stats.map_attempts, calls["n"])
        self.assertGreater(stats.map_calls_succeeded, 0)

    def test_zero_acknowledgement_degrades_batch(self) -> None:
        chunks = _tiny_chunks(4)
        corpus = _synthetic_corpus(chunks)
        empty_batches = {"n": 0}

        def handler(user: str) -> str:
            ids = _chunk_ids_in_prompt(user)
            if len(ids) >= 4:
                empty_batches["n"] += 1
                return json.dumps({"chunks": []})
            return _map_chunks_json(ids)

        _review, coverage, _store, stats = self._run(corpus, self._ack_model(handler))
        self.assertTrue(coverage.complete)
        self.assertGreaterEqual(empty_batches["n"], 1)
        self.assertGreaterEqual(stats.map_batches_split, 1)
        self.assertTrue(any("acknowledged 0/" in note for note in stats.notes))

    def test_hallucinated_only_acknowledgement_degrades_batch(self) -> None:
        chunks = _tiny_chunks(2)
        corpus = _synthetic_corpus(chunks)
        hallucinated = {"n": 0}

        def handler(user: str) -> str:
            ids = _chunk_ids_in_prompt(user)
            if len(ids) > 1:
                hallucinated["n"] += 1
                return _map_chunks_json(["NOT_REAL"])
            return _map_chunks_json(ids)

        _review, coverage, _store, stats = self._run(corpus, self._ack_model(handler))
        self.assertTrue(coverage.complete)
        self.assertGreaterEqual(hallucinated["n"], 1)
        self.assertGreaterEqual(stats.map_batches_split, 1)
        self.assertNotIn("NOT_REAL", coverage.uncovered_chunk_ids)

    def test_partial_acknowledgement_retries_only_missing_chunks(self) -> None:
        chunks = _tiny_chunks(4)
        corpus = _synthetic_corpus(chunks)
        requests: list[list[str]] = []

        def handler(user: str) -> str:
            ids = _chunk_ids_in_prompt(user)
            requests.append(ids)
            if len(ids) >= 4:
                return _map_chunks_json(ids[:3])
            return _map_chunks_json(ids)

        _review, coverage, _store, stats = self._run(corpus, self._ack_model(handler))
        self.assertTrue(coverage.complete)
        self.assertGreaterEqual(len(requests), 2)
        self.assertEqual(set(requests[0]), {chunk.id for chunk in chunks})
        later = [item for batch in requests[1:] for item in batch]
        self.assertEqual(set(later), {"C4"})
        self.assertNotIn("C1", later)
        self.assertNotIn("C2", later)
        self.assertNotIn("C3", later)
        self.assertGreaterEqual(stats.map_partial_responses, 1)

    def test_successful_partial_evidence_is_preserved(self) -> None:
        chunks = _tiny_chunks(2)
        corpus = _synthetic_corpus(chunks)
        requests: list[list[str]] = []

        def handler(user: str) -> str:
            ids = _chunk_ids_in_prompt(user)
            requests.append(ids)
            if "C1" in ids and "C2" in ids:
                return _map_chunks_json(
                    ["C1"],
                    findings=[
                        {
                            "id": "F1",
                            "severity": "MAJOR",
                            "path": "c1.c",
                            "body": "from C1 only",
                            "confidence": "LIKELY",
                        }
                    ],
                )
            return _map_chunks_json(ids)

        _review, coverage, store, _stats = self._run(corpus, self._ack_model(handler))
        self.assertTrue(coverage.complete)
        finding = _by_local_id(store, "F1")
        self.assertEqual(finding.body, "from C1 only")
        self.assertEqual(sum(batch.count("C1") for batch in requests), 1)
        later = [item for batch in requests[1:] for item in batch]
        self.assertNotIn("C1", later)
        self.assertIn("C2", later)

    def test_single_pathological_chunk_does_not_poison_siblings(self) -> None:
        chunks = _tiny_chunks(4)
        corpus = _synthetic_corpus(chunks)

        def handler(user: str) -> str:
            ids = _chunk_ids_in_prompt(user)
            if "C3" in ids:
                return "not json"
            return _map_chunks_json(ids)

        review, coverage, _store, stats = self._run(corpus, self._ack_model(handler))
        self.assertFalse(coverage.complete)
        self.assertEqual(coverage.uncovered_chunk_ids, ["C3"])
        _assert_unsynthesized_fallback(self, review)
        self.assertIn("1 context chunk(s)", review["body"])
        self.assertNotIn("`C1`", review["body"])
        self.assertIn("`C3`", review["body"])
        self.assertEqual(stats.map_chunks_acknowledged, 3)
        self.assertEqual(stats.map_chunks_uncovered, 1)
        self.assertEqual(stats.synthesis_calls, 0)

    def test_map_attempt_cap_is_global(self) -> None:
        chunks = _tiny_chunks(8)
        corpus = _synthetic_corpus(chunks)

        def boom(_system: str, _user: str) -> str:
            raise RuntimeError("always fail")

        with mock.patch.object(rp, "MAX_MAP_ATTEMPTS", 5):
            review, coverage, _store, stats = self._run(corpus, boom)
        self.assertEqual(stats.map_attempts, 5)
        self.assertLessEqual(stats.map_attempts, 5)
        self.assertEqual(stats.map_calls_succeeded, 0)
        self.assertFalse(coverage.complete)
        self.assertEqual(len(coverage.uncovered_chunk_ids), 8)
        self.assertEqual(review["event"], "COMMENT")
        self.assertTrue(any("attempt budget exhausted" in note for note in stats.notes))

    def test_provider_failures_count_as_attempts(self) -> None:
        chunks = _tiny_chunks(2)
        corpus = _synthetic_corpus(chunks)
        calls = {"n": 0}

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system and "Context requests" not in user:
                calls["n"] += 1
                raise RuntimeError("nope")
            if "merge-warden-reduce" in system:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            raise AssertionError("synthesis must not run")

        with mock.patch.object(rp, "MAX_MAP_ATTEMPTS", 3):
            _review, _coverage, _store, stats = self._run(corpus, fake)
        self.assertEqual(stats.map_attempts, calls["n"])
        self.assertEqual(stats.map_attempts, stats.map_provider_failures)
        self.assertEqual(stats.map_calls_succeeded, 0)

    def test_malformed_responses_count_as_attempts(self) -> None:
        chunks = _tiny_chunks(2)
        corpus = _synthetic_corpus(chunks)

        def handler(_user: str) -> str:
            return "still not json"

        with mock.patch.object(rp, "MAX_MAP_ATTEMPTS", 3):
            _review, _coverage, _store, stats = self._run(
                corpus, self._ack_model(handler)
            )
        self.assertEqual(stats.map_attempts, stats.map_non_json_responses)
        self.assertEqual(stats.map_calls_succeeded, 0)
        self.assertLessEqual(stats.map_attempts, 3)

    def test_internal_http_retries_do_not_multiply_logical_attempts(self) -> None:
        """HTTP retries inside one call_model() are one logical map attempt."""
        corpus = _synthetic_corpus(_tiny_chunks(1))
        counts = {"calls": 0, "http": 0}

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system and "Context requests" not in user:
                counts["calls"] += 1
                counts["http"] += 3
                return _map_chunks_json(_chunk_ids_in_prompt(user))
            if "merge-warden-reduce" in system:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps({"event": "COMMENT", "body": "# COMMENT\n", "comments": []})

        _review, coverage, _store, stats = self._run(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertEqual(counts["calls"], 1)
        self.assertEqual(counts["http"], 3)
        self.assertEqual(stats.map_attempts, 1)
        self.assertEqual(stats.map_calls, 1)

    def test_architecture_document_sections_coalesce_without_losing_ids(self) -> None:
        text = _heading_doc("Readme", 21)
        corpus = build_review_corpus(
            _inputs(arch_docs=[("README.md", text)], diff="+ok\n")
        )
        arch = [
            chunk
            for chunk in corpus.reviewable_chunks
            if chunk.kind == "arch" and chunk.source == "README.md"
        ]
        self.assertEqual(len(arch), 1)
        members = arch[0].member_ids
        self.assertGreaterEqual(len(members), 21)
        self.assertTrue(all(item.startswith("arch:README.md:") for item in members))
        self.assertLessEqual(arch[0].size, DEFAULT_MAX_SINGLE_CHUNK_CHARS)
        self.assertLessEqual(arch[0].size, DEFAULT_ARCH_COALESCE_CHARS)

        recorder = _ReviewRecorder()
        review, coverage, _store, _stats = self._run(corpus, recorder)
        self.assertTrue(coverage.complete)
        for member_id in members:
            self.assertNotIn(member_id, coverage.uncovered_chunk_ids)
        supplied = [
            chunk_id
            for message in recorder.map_messages
            for chunk_id in _chunk_ids_in_prompt(message)
        ]
        self.assertIn(arch[0].id, supplied)
        self.assertEqual(review["event"], "COMMENT")

    def test_architecture_coalesce_respects_max_single_chunk_size(self) -> None:
        limit = 220
        text = _heading_doc("Readme", 12, filler="x" * 40)
        corpus = build_review_corpus(
            _inputs(arch_docs=[("README.md", text)]),
            max_single_chunk_chars=limit,
        )
        arch = [chunk for chunk in corpus.chunks if chunk.kind == "arch"]
        self.assertGreater(len(arch), 1)
        for chunk in arch:
            self.assertLessEqual(chunk.size, limit)
        members = [member for chunk in arch for member in chunk.member_ids]
        self.assertGreaterEqual(len(members), 12)

    def test_changed_code_chunks_remain_independent_of_arch_coalesce(self) -> None:
        diff = (
            "diff --git a/src/a.c b/src/a.c\n"
            "--- a/src/a.c\n"
            "+++ b/src/a.c\n"
            "@@ -1,1 +1,1 @@\n"
            "-old-a\n"
            "+new-a\n"
            "diff --git a/src/b.c b/src/b.c\n"
            "--- a/src/b.c\n"
            "+++ b/src/b.c\n"
            "@@ -1,1 +1,1 @@\n"
            "-old-b\n"
            "+new-b\n"
        )
        text = _heading_doc("Guide", 12)
        corpus = build_review_corpus(
            _inputs(
                arch_docs=[("CONTRIBUTING.md", text)],
                diff=diff,
                files=[
                    {"filename": "src/a.c", "status": "modified", "additions": 1, "deletions": 1},
                    {"filename": "src/b.c", "status": "modified", "additions": 1, "deletions": 1},
                ],
                file_contents={"src/a.c": "int a;\n", "src/b.c": "int b;\n"},
            )
        )
        diff_chunks = [chunk for chunk in corpus.reviewable_chunks if chunk.kind == "diff"]
        sources = {chunk.source for chunk in diff_chunks}
        self.assertIn("src/a.c", sources)
        self.assertIn("src/b.c", sources)
        self.assertFalse(
            any("|" in chunk.source and chunk.kind == "diff" for chunk in corpus.reviewable_chunks)
        )
        file_chunks = [chunk for chunk in corpus.source_chunks if chunk.kind == "file"]
        self.assertTrue(any(chunk.source == "src/a.c" for chunk in file_chunks))
        self.assertTrue(any(chunk.source == "src/b.c" for chunk in file_chunks))
        self.assertFalse(
            any(chunk.kind == "file" and chunk.source in {"src/a.c", "src/b.c"} for chunk in corpus.reviewable_chunks)
        )

    def test_failure_diagnostics_appear_in_incomplete_review(self) -> None:
        chunks = _tiny_chunks(10)
        corpus = _synthetic_corpus(chunks)
        fail = {"C9", "C10"}

        def handler(user: str) -> str:
            ids = _chunk_ids_in_prompt(user)
            if any(chunk_id in fail for chunk_id in ids):
                return "not json"
            return _map_chunks_json(ids)

        review, coverage, _store, stats = self._run(corpus, self._ack_model(handler))
        self.assertFalse(coverage.complete)
        self.assertEqual(set(coverage.uncovered_chunk_ids), fail)
        _assert_unsynthesized_fallback(self, review)
        self.assertIn("8 / 10 context chunks analyzed", review["body"])
        self.assertIn("Map failures:", review["body"])
        self.assertIn("non-JSON", review["body"])
        self.assertIn("planned map batch", stats.footer())
        self.assertIn("map attempt", stats.footer())
        self.assertIn("8/10 primary chunks acknowledged", stats.footer())
        self.assertIn("coverage incomplete", stats.footer())

    def test_failure_diagnostics_are_bounded(self) -> None:
        chunks = _tiny_chunks(1)
        corpus = _synthetic_corpus(chunks)

        def handler(_user: str) -> str:
            return "not json"

        _review, _coverage, _store, stats = self._run(corpus, self._ack_model(handler))
        stats.notes = [f"map batch synthetic:{index} returned non-JSON evidence" for index in range(200)]
        body = incomplete_coverage_body(
            corpus.coverage,
            analyzed=0,
            total=1,
            failure_notes=stats.notes,
        )
        self.assertEqual(MAX_FAILURE_NOTES_IN_REVIEW, 10)
        self.assertLessEqual(body.count("map batch synthetic:"), MAX_FAILURE_NOTES_IN_REVIEW)
        self.assertIn("… 190 more", body)
        self.assertNotIn("map batch synthetic:50", body)

    def test_provider_error_bodies_are_not_exposed(self) -> None:
        chunks = _tiny_chunks(1)
        corpus = _synthetic_corpus(chunks)
        secret = "Authorization: Bearer SECRET_TOKEN_ABC"

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system and "Context requests" not in user:
                raise RuntimeError(secret + " raw-provider-body " + ("Z" * 4000))
            if "merge-warden-reduce" in system:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            raise AssertionError("synthesis must not run")

        review, coverage, _store, stats = self._run(corpus, fake)
        self.assertFalse(coverage.complete)
        self.assertNotIn("SECRET_TOKEN_ABC", review["body"])
        self.assertNotIn("Bearer SECRET", review["body"])
        self.assertNotIn("Z" * 200, review["body"])
        joined = "\n".join(stats.notes)
        self.assertNotIn("SECRET_TOKEN_ABC", joined)
        self.assertIn("[redacted]", sanitize_failure_note(secret))

    def test_complete_coverage_still_permits_synthesis(self) -> None:
        chunks = _tiny_chunks(20)
        corpus = _synthetic_corpus(chunks)
        recorder = _ReviewRecorder(synthesis_event="APPROVE", synthesis_body="# APPROVE\n")
        review, coverage, _store, stats = self._run(corpus, recorder)
        self.assertTrue(coverage.complete)
        self.assertGreater(stats.batches, 1)
        self.assertGreaterEqual(stats.map_attempts, 3)
        self.assertEqual(stats.synthesis_calls, 1)
        self.assertEqual(review["event"], "APPROVE")
        for message in recorder.map_messages:
            self.assertLessEqual(len(_chunk_ids_in_prompt(message)), MAX_MAP_CHUNKS_PER_CALL)

    def test_incomplete_coverage_still_cannot_approve(self) -> None:
        chunks = _tiny_chunks(3)
        corpus = _synthetic_corpus(chunks)

        def handler(user: str) -> str:
            ids = _chunk_ids_in_prompt(user)
            if "C3" in ids:
                return "not json"
            return _map_chunks_json(ids)

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system and "Context requests" not in user:
                return handler(user)
            if "merge-warden-reduce" in system:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "APPROVE", "body": "# APPROVE\n\nLooks good.\n", "comments": []}
            )

        review, coverage, _store, stats = self._run(corpus, fake)
        self.assertFalse(coverage.complete)
        _assert_unsynthesized_fallback(self, review)
        self.assertEqual(stats.synthesis_calls, 0)

    def test_tail_sentinel_is_mapped_or_explicitly_uncovered(self) -> None:
        sentinel = "TAIL_SENTINEL_123"
        chunks = _tiny_chunks(5)
        chunks[-1] = _chunk("C5", "c5.c", f"tiny-5\n{sentinel}\n")
        corpus = _synthetic_corpus(chunks)
        recorder = _ReviewRecorder()
        _review, coverage, _store, _stats = self._run(corpus, recorder)
        joined = "\n".join(recorder.map_messages)
        self.assertTrue(coverage.complete)
        self.assertIn(sentinel, joined)
        self.assertIn(sentinel, chunks[-1].text)

    def test_validation_is_not_bound_by_map_chunk_fanout(self) -> None:
        map_chunk = _chunk(
            "diff:src/foo.c:1",
            "src/foo.c",
            '+#include "include/foo.h"\n',
            kind="diff",
        )
        matching = [
            _chunk(
                f"file:include/foo.h:{index}",
                "include/foo.h",
                f"/* part {index} */\nint field_{index};\n",
            )
            for index in range(1, 11)
        ]
        corpus = _synthetic_corpus([map_chunk])
        corpus.source_chunks = matching
        recorder = _ReviewRecorder(
            findings=[_likely_foo_finding()],
            needs_context=[{"path": "include/foo.h", "reason": "cross-context check"}],
        )
        _review, coverage, _store, stats = self._run(corpus, recorder)
        self.assertTrue(coverage.complete)
        self.assertTrue(recorder.validation_messages)
        self.assertGreater(len(matching), MAX_MAP_CHUNKS_PER_CALL)
        self.assertGreaterEqual(
            max(len(_chunk_ids_in_prompt(message)) for message in recorder.validation_messages),
            MAX_MAP_CHUNKS_PER_CALL + 1,
        )
        self.assertEqual(stats.validation_chunks_acknowledged, len(matching))

    def test_brainrot_shaped_small_chunk_corpus_recovers(self) -> None:
        arch_docs = [
            ("AGENTS.md", _heading_doc("Agent", 18)),
            ("CONTRIBUTING.md", _heading_doc("Contributing", 21)),
            ("README.md", _heading_doc("Readme", 16)),
        ]
        files = [
            {
                "filename": f"src/mod_{index}.c",
                "status": "modified",
                "additions": 1,
                "deletions": 0,
            }
            for index in range(1, 9)
        ]
        file_contents = {
            item["filename"]: f"int f_{item['filename']}(void) {{ return 1; }}\n"
            for item in files
        }
        file_contents["src/mod_8.c"] += "TAIL_SENTINEL_123\n"
        diff_parts = []
        for item in files:
            path = item["filename"]
            diff_parts.append(
                f"diff --git a/{path} b/{path}\n"
                f"--- a/{path}\n"
                f"+++ b/{path}\n"
                f"@@ -1,0 +1,1 @@\n"
                f"+int f_{path}(void) {{ return 1; }}\n"
            )
        corpus = build_review_corpus(
            _inputs(
                pr=_pr(body=_heading_doc("PR", 6, filler="change")),
                arch_docs=arch_docs,
                files=files,
                file_contents=file_contents,
                diff="".join(diff_parts),
            )
        )
        member_ids = [
            member
            for chunk in corpus.reviewable_chunks
            for member in chunk.member_ids
        ]
        source_member_ids = [
            member
            for chunk in corpus.source_chunks
            for member in chunk.member_ids
        ]
        self.assertTrue(source_member_ids)
        self.assertTrue(all(not member.startswith("file:") for member in member_ids))
        self.assertTrue(all(member.startswith("file:") for member in source_member_ids))
        self.assertGreater(len(corpus.reviewable_chunks), MAX_MAP_CHUNKS_PER_CALL)
        self.assertIn("TAIL_SENTINEL_123", "\n".join(chunk.text for chunk in corpus.source_chunks))
        self.assertNotIn("TAIL_SENTINEL_123", "\n".join(chunk.text for chunk in corpus.reviewable_chunks))

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system:
                ids = _chunk_ids_in_prompt(user)
                if "Context requests" in user:
                    return _map_chunks_json(ids)
                self.assertLessEqual(len(ids), MAX_MAP_CHUNKS_PER_CALL)
                if len(ids) > MAX_MAP_CHUNKS_PER_CALL:
                    return "not json"
                return _map_chunks_json(ids)
            if "merge-warden-reduce" in system:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        review, coverage, _store, stats = self._run(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertLessEqual(stats.map_attempts, MAX_MAP_ATTEMPTS)
        self.assertGreater(stats.batches, 1)
        self.assertEqual(stats.synthesis_calls, 1)
        self.assertEqual(review["event"], "COMMENT")


class ParallelMapSchedulerTests(unittest.TestCase):
    def _run(self, corpus: ReviewCorpus, fake, **kwargs):
        return _run_hierarchical(corpus, fake, **kwargs)

    def _pipeline(self, handler):
        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system:
                if "Context requests" in user or VALIDATION_STAGE_TOKEN in user:
                    return _map_chunks_json(_chunk_ids_in_prompt(user))
                return handler(user)
            if "merge-warden-reduce" in system:
                return json.dumps({"keep": [], "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        return fake

    def test_normalize_map_concurrency_clamps_to_conservative_bounds(self) -> None:
        self.assertEqual(DEFAULT_MAP_CONCURRENCY, 4)
        self.assertEqual(MAX_MAP_CONCURRENCY, 8)
        self.assertEqual(normalize_map_concurrency(None), 4)
        self.assertEqual(normalize_map_concurrency(1), 1)
        self.assertEqual(normalize_map_concurrency(4), 4)
        self.assertEqual(normalize_map_concurrency(100), 8)
        self.assertEqual(normalize_map_concurrency(0), 1)
        self.assertEqual(normalize_map_concurrency(-3), 1)

    def test_validation_user_message_carries_stage_token(self) -> None:
        corpus = _synthetic_corpus(_tiny_chunks(1))
        message = format_validation_user_message(
            corpus,
            [],
            corpus.reviewable_chunks,
            [],
        )
        self.assertIn(f"<!-- {VALIDATION_STAGE_TOKEN} -->", message)
        self.assertTrue(message.lstrip().startswith(rp.UNTRUSTED_CONTEXT_BANNER[:20]))

    def test_independent_map_calls_overlap(self) -> None:
        corpus = _synthetic_corpus(_tiny_chunks(24))
        barrier = threading.Barrier(3, timeout=5)
        overlapped = {"ok": False, "error": None}

        def handler(user: str) -> str:
            ids = _chunk_ids_in_prompt(user)
            try:
                barrier.wait()
                overlapped["ok"] = True
            except threading.BrokenBarrierError as exc:
                overlapped["error"] = exc
            return _map_chunks_json(ids)

        review, coverage, _store, stats = self._run(
            corpus, self._pipeline(handler), map_concurrency=4
        )
        self.assertTrue(coverage.complete)
        self.assertGreaterEqual(stats.batches, 3)
        self.assertTrue(overlapped["ok"])
        self.assertIsNone(overlapped["error"])
        self.assertEqual(review["event"], "COMMENT")

    def test_concurrency_never_exceeds_configured_limit(self) -> None:
        corpus = _synthetic_corpus(_tiny_chunks(32))
        current = 0
        max_inflight = 0
        lock = threading.Lock()

        def handler(user: str) -> str:
            nonlocal current, max_inflight
            with lock:
                current += 1
                max_inflight = max(max_inflight, current)
            time.sleep(0.05)
            with lock:
                current -= 1
            return _map_chunks_json(_chunk_ids_in_prompt(user))

        _review, coverage, _store, stats = self._run(
            corpus, self._pipeline(handler), map_concurrency=2
        )
        self.assertTrue(coverage.complete)
        self.assertEqual(max_inflight, 2)
        self.assertGreaterEqual(stats.batches, 4)
        self.assertLessEqual(current, 0)

    def test_results_are_ingested_in_sequence_order(self) -> None:
        corpus = _synthetic_corpus(_tiny_chunks(24))
        first_gate = threading.Event()
        later = threading.Barrier(2, timeout=5)
        recorded_later = threading.Barrier(2, timeout=5)
        completion: list[str] = []
        lock = threading.Lock()

        def handler(user: str) -> str:
            ids = _chunk_ids_in_prompt(user)
            lead = ids[0]
            payload = _map_chunks_json(
                ids,
                needs_context=[{"path": f"from-{lead}", "reason": "order"}],
            )
            if lead == "C1":
                if not first_gate.wait(timeout=5):
                    raise AssertionError("first batch was not released")
                with lock:
                    completion.append(lead)
                return payload
            later.wait()
            with lock:
                completion.append(lead)
            recorded_later.wait()
            if lead == "C17":
                first_gate.set()
            return payload

        _review, coverage, store, _stats = self._run(
            corpus, self._pipeline(handler), map_concurrency=4
        )
        self.assertTrue(coverage.complete)
        self.assertEqual(set(completion[:2]), {"C9", "C17"})
        self.assertEqual(completion[-1], "C1")
        self.assertEqual(
            [need.path for need in store.needs_context],
            ["from-C1", "from-C9", "from-C17"],
        )

    def test_deadline_stops_scheduling_new_batches(self) -> None:
        corpus = _synthetic_corpus(_tiny_chunks(64))
        calls: list[list[str]] = []
        lock = threading.Lock()

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system and VALIDATION_STAGE_TOKEN not in user:
                with lock:
                    calls.append(_chunk_ids_in_prompt(user))
                raise PipelineDeadlineExceeded("provider cutoff reached during map")
            raise AssertionError("no later pipeline stage should run")

        review, coverage, _store, stats = self._run(
            corpus, fake, map_concurrency=4
        )
        self.assertEqual(len(calls), 4)
        self.assertEqual(stats.map_attempts, 4)
        self.assertEqual(stats.map_batches_split, 0)
        self.assertTrue(stats.deadline_exhausted)
        self.assertFalse(coverage.complete)
        _assert_unsynthesized_fallback(self, review)
        self.assertIn("wall-clock review deadline", review["body"])

    def test_deadline_does_not_trigger_retry_or_split_storm(self) -> None:
        corpus = _synthetic_corpus(_tiny_chunks(24))
        calls = {"n": 0}
        lock = threading.Lock()

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system and VALIDATION_STAGE_TOKEN not in user:
                with lock:
                    calls["n"] += 1
                raise PipelineDeadlineExceeded("provider cutoff reached during map")
            raise AssertionError("no later pipeline stage should run")

        _review, _coverage, _store, stats = self._run(
            corpus, fake, map_concurrency=4
        )
        self.assertEqual(calls["n"], 3)
        self.assertEqual(stats.map_attempts, 3)
        self.assertEqual(stats.map_batches_split, 0)
        self.assertEqual(stats.map_calls_succeeded, 0)
        self.assertTrue(stats.deadline_exhausted)

    def test_deadline_preserves_successful_sibling_acknowledgements(self) -> None:
        corpus = _synthetic_corpus(_tiny_chunks(16))
        success_ids = {f"C{index}" for index in range(1, 9)}
        deadline_ids = {f"C{index}" for index in range(9, 17)}

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system and VALIDATION_STAGE_TOKEN not in user:
                ids = _chunk_ids_in_prompt(user)
                if "C1" in ids:
                    return _map_chunks_json(
                        ids,
                        findings=[
                            {
                                "id": "F_DEADLINE_SURVIVOR",
                                "severity": "MAJOR",
                                "path": "c1.c",
                                "side": "RIGHT",
                                "line": 1,
                                "body": "deadline survivor finding",
                                "confidence": "LIKELY",
                            }
                        ],
                    )
                raise PipelineDeadlineExceeded("provider cutoff reached during map")
            raise AssertionError("no later pipeline stage should run")

        review, coverage, store, stats = self._run(
            corpus, fake, map_concurrency=2
        )
        self.assertEqual(review["event"], "COMMENT")
        self.assertFalse(coverage.complete)
        self.assertEqual(set(coverage.uncovered_chunk_ids), deadline_ids)
        self.assertTrue(success_ids.isdisjoint(coverage.uncovered_chunk_ids))
        self.assertEqual(stats.map_chunks_acknowledged, len(success_ids))
        self.assertEqual(stats.map_chunks_uncovered, len(deadline_ids))
        self.assertEqual(stats.map_calls_succeeded, 1)
        self.assertEqual(stats.map_batches_split, 0)
        self.assertTrue(stats.deadline_exhausted)
        _assert_unsynthesized_fallback(self, review)
        self.assertEqual(
            [item.body for item in store.kept_findings()],
            ["deadline survivor finding"],
        )
        self.assertNotIn("deadline survivor finding", review["body"])
        self.assertEqual(
            _by_local_id(store, "F_DEADLINE_SURVIVOR").body,
            "deadline survivor finding",
        )

    def test_failed_batch_does_not_poison_successful_siblings(self) -> None:
        corpus = _synthetic_corpus(_tiny_chunks(16))
        poison = {f"C{index}" for index in range(9, 17)}

        def handler(user: str) -> str:
            ids = _chunk_ids_in_prompt(user)
            if poison.intersection(ids):
                raise RuntimeError("provider unavailable for sibling batch")
            return _map_chunks_json(ids)

        review, coverage, _store, stats = self._run(
            corpus, self._pipeline(handler), map_concurrency=4
        )
        self.assertFalse(coverage.complete)
        self.assertEqual(set(coverage.uncovered_chunk_ids), poison)
        self.assertEqual(stats.map_chunks_acknowledged, 8)
        self.assertGreaterEqual(stats.map_provider_failures, 1)
        self.assertGreater(stats.map_calls_succeeded, 0)
        self.assertEqual(review["event"], "COMMENT")
        self.assertIn("`C9`", review["body"])
        self.assertNotIn("`C1`", review["body"])


class ParallelValidationSchedulerTests(unittest.TestCase):
    def _paths_corpus(self, paths: list[str]) -> ReviewCorpus:
        map_chunk = _chunk(
            "diff:src/foo.c:1", "src/foo.c", "+int main(void);\n", kind="diff"
        )
        corpus = _synthetic_corpus(
            [map_chunk],
            index="Changed files:\n- src/foo.c +1 -0\n",
        )
        corpus.source_chunks = [
            _chunk(f"file:{path}:1", path, f"/* {path} */\nint field;\n")
            for path in paths
        ]
        return corpus

    def _specs(
        self, items: list[tuple[str, str, str, str]]
    ) -> tuple[list[dict], list[dict]]:
        findings = [
            {
                "id": finding_id,
                "severity": severity,
                "path": "src/foo.c",
                "body": f"{severity} {confidence} candidate for {path}",
                "confidence": confidence,
                "evidence": [],
            }
            for finding_id, severity, confidence, path in items
        ]
        needs = [
            {
                "path": path,
                "reason": f"Need context for {finding_id}",
                "finding_ids": [finding_id],
            }
            for finding_id, _severity, _confidence, path in items
        ]
        return findings, needs

    def _pipeline(
        self,
        *,
        findings: list[dict],
        needs_context: list,
        on_validation=None,
        on_reduce=None,
        keep_ids: list[str] | None = None,
    ):
        state = {"map": 0, "validation": 0}
        validation_messages: list[str] = []
        validation_threads: list[int] = []
        lock = threading.Lock()
        kept = keep_ids if keep_ids is not None else [item["id"] for item in findings]

        def fake(system: str, user: str) -> str:
            if "merge-warden-map" in system:
                ids = _chunk_ids_in_prompt(user)
                if "Context requests" in user or VALIDATION_STAGE_TOKEN in user:
                    with lock:
                        validation_messages.append(user)
                        state["validation"] += 1
                        n = state["validation"]
                    validation_threads.append(threading.get_ident())
                    if on_validation is not None:
                        return on_validation(n, user, ids)
                    return _map_chunks_json(ids)
                extras: dict = {}
                with lock:
                    first = state["map"] == 0
                    state["map"] += 1
                if first:
                    extras["findings"] = findings
                    extras["needs_context"] = needs_context
                return _map_chunks_json(ids, **extras)
            if "merge-warden-reduce" in system:
                if on_reduce is not None:
                    return on_reduce(system, user)
                return json.dumps({"keep": kept, "reject": [], "merge": []})
            return json.dumps(
                {"event": "COMMENT", "body": "# COMMENT\n", "comments": []}
            )

        fake.state = state  # type: ignore[attr-defined]
        fake.validation_messages = validation_messages  # type: ignore[attr-defined]
        fake.validation_threads = validation_threads  # type: ignore[attr-defined]
        return fake

    def test_normalize_validation_concurrency_clamps_to_conservative_bounds(
        self,
    ) -> None:
        self.assertEqual(DEFAULT_VALIDATION_CONCURRENCY, 2)
        self.assertEqual(MAX_VALIDATION_CONCURRENCY, 4)
        self.assertNotEqual(DEFAULT_VALIDATION_CONCURRENCY, DEFAULT_MAP_CONCURRENCY)
        self.assertLess(MAX_VALIDATION_CONCURRENCY, MAX_MAP_CONCURRENCY)
        self.assertEqual(normalize_validation_concurrency(None), 2)
        self.assertEqual(normalize_validation_concurrency(1), 1)
        self.assertEqual(normalize_validation_concurrency(2), 2)
        self.assertEqual(normalize_validation_concurrency(4), 4)
        self.assertEqual(normalize_validation_concurrency(100), 4)
        self.assertEqual(normalize_validation_concurrency(0), 1)
        self.assertEqual(normalize_validation_concurrency(-3), 1)
        self.assertEqual(normalize_validation_concurrency("nope"), 2)  # type: ignore[arg-type]

    def test_validation_path_sort_key_orders_severity_then_impact(self) -> None:
        blocking_question = [
            _finding("B", severity="BLOCKING", confidence="QUESTION")
        ]
        major_likely = [_finding("M", severity="MAJOR", confidence="LIKELY")]
        minor_likely = [_finding("N", severity="MINOR", confidence="LIKELY")]
        major_question = [_finding("Q", severity="MAJOR", confidence="QUESTION")]
        # Severity beats original order and within-severity impact.
        self.assertLess(
            validation_path_sort_key(blocking_question, 9),
            validation_path_sort_key(major_likely, 0),
        )
        self.assertLess(
            validation_path_sort_key(major_likely, 9),
            validation_path_sort_key(minor_likely, 0),
        )
        self.assertLess(
            validation_path_sort_key(major_likely, 5),
            validation_path_sort_key(major_question, 0),
        )
        self.assertEqual(validation_path_sort_key(major_likely, 3)[2], 3)

    def test_validation_path_sort_key_ranks_blocker_alias_with_blocking(self) -> None:
        blocker = [_finding("B", severity="BLOCKER", confidence="LIKELY")]
        blocking = [_finding("K", severity="BLOCKING", confidence="LIKELY")]
        minor = [_finding("N", severity="MINOR", confidence="LIKELY")]
        self.assertLess(
            validation_path_sort_key(blocker, 9),
            validation_path_sort_key(minor, 0),
        )
        self.assertEqual(
            validation_path_sort_key(blocker, 3)[:2],
            validation_path_sort_key(blocking, 3)[:2],
        )
        self.assertFalse(
            validation_path_sort_key(minor, 0)
            < validation_path_sort_key(blocker, 9),
            "MINOR must not be scheduled ahead of a BLOCKER alias",
        )

    def test_plan_validation_tasks_reorders_minor_ahead_of_blocking(self) -> None:
        store = EvidenceStore()
        store.findings["minor"] = _finding("minor", severity="MINOR")
        store.findings["blocking"] = _finding("blocking", severity="BLOCKING")
        store.needs_context = [
            rp.ContextNeed(
                path="include/minor.h",
                reason="low value",
                finding_ids=["minor"],
            ),
            rp.ContextNeed(
                path="include/blocking.h",
                reason="root cause",
                finding_ids=["blocking"],
            ),
        ]
        tasks = plan_validation_tasks(store)
        self.assertEqual(
            [task.path for task in tasks],
            ["include/blocking.h", "include/minor.h"],
        )

    def test_plan_validation_tasks_orders_blocker_alias_ahead_of_minor(self) -> None:
        store = EvidenceStore()
        store.findings["blocker"] = _finding("blocker", severity="BLOCKER")
        store.findings["minor"] = _finding("minor", severity="MINOR")
        store.needs_context = [
            rp.ContextNeed(
                path="include/minor.h",
                reason="low value",
                finding_ids=["minor"],
            ),
            rp.ContextNeed(
                path="include/blocker.h",
                reason="root cause",
                finding_ids=["blocker"],
            ),
        ]
        tasks = plan_validation_tasks(store)
        self.assertEqual(
            [task.path for task in tasks],
            ["include/blocker.h", "include/minor.h"],
        )

    def test_plan_validation_tasks_orders_by_joined_merge_class_severity(self) -> None:
        store = EvidenceStore()
        store.findings["minor_canon"] = _finding(
            "minor_canon", severity="MINOR", confidence="QUESTION"
        )
        store.findings["blocking_member"] = _finding(
            "blocking_member", severity="BLOCKING", confidence="LIKELY"
        )
        store.findings["plain_minor"] = _finding(
            "plain_minor", severity="MINOR", confidence="LIKELY"
        )
        store.merged_into["blocking_member"] = "minor_canon"
        store.kept = {"minor_canon", "plain_minor"}
        store.reduced = True
        store.needs_context = [
            rp.ContextNeed(
                path="include/minor.h",
                reason="low value",
                finding_ids=["plain_minor"],
            ),
            rp.ContextNeed(
                path="include/critical.h",
                reason="root cause",
                finding_ids=["minor_canon"],
            ),
        ]
        tasks = plan_validation_tasks(store)
        self.assertEqual(
            [task.path for task in tasks],
            ["include/critical.h", "include/minor.h"],
        )
        self.assertEqual(store.findings["minor_canon"].severity, "MINOR")

    def test_plan_validation_tasks_does_not_use_prompt_confidence_demotion(
        self,
    ) -> None:
        store = EvidenceStore()
        store.findings["confirmed"] = _finding(
            "confirmed", severity="MAJOR", confidence="CONFIRMED"
        )
        store.findings["likely"] = _finding(
            "likely", severity="MAJOR", confidence="LIKELY"
        )
        store.needs_context = [
            rp.ContextNeed(
                path="include/confirmed.h",
                reason="already confirmed, still needs context",
                finding_ids=["confirmed"],
            ),
            rp.ContextNeed(
                path="include/likely.h",
                reason="can become CONFIRMED",
                finding_ids=["likely"],
            ),
        ]
        tasks = plan_validation_tasks(store)
        self.assertEqual(
            [task.path for task in tasks],
            ["include/likely.h", "include/confirmed.h"],
        )

    def test_plan_validation_tasks_ranks_every_merge_class_not_prompt_slice(
        self,
    ) -> None:
        store = EvidenceStore()
        store.findings["plain"] = _finding("plain", severity="MINOR")
        store.needs_context = [
            rp.ContextNeed(
                path="include/minor.h",
                reason="low value",
                finding_ids=["plain"],
            )
        ]
        for index in range(1, 14):
            finding_id = f"crowd{index}"
            severity = "BLOCKING" if index == 13 else "MINOR"
            store.findings[finding_id] = _finding(finding_id, severity=severity)
            store.needs_context.append(
                rp.ContextNeed(
                    path="include/crowded.h",
                    reason="one of many",
                    finding_ids=[finding_id],
                )
            )
        tasks = plan_validation_tasks(store)
        self.assertEqual(tasks[0].path, "include/crowded.h")
        self.assertEqual(
            len(tasks[0].related_for_prompt), rp.MAX_VALIDATION_PROMPT_FINDINGS
        )
        self.assertTrue(
            any(item.severity == "BLOCKING" for item in tasks[0].related_for_prompt),
            "the BLOCKING finding that ranked this path must stay a prompt candidate",
        )
        self.assertEqual(tasks[0].related_for_prompt[0].id, "crowd13")
        self.assertEqual(store.findings["crowd13"].severity, "BLOCKING")
        self.assertEqual(
            [task.path for task in tasks],
            ["include/crowded.h", "include/minor.h"],
        )

    def test_plan_validation_tasks_collapses_path_aliases(self) -> None:
        store = EvidenceStore()
        store.findings["c1/F1"] = _finding("c1/F1", severity="BLOCKING")
        store.kept.add("c1/F1")
        store.reduced = True
        store.needs_context = [
            rp.ContextNeed(
                path="foo.h", reason="r", from_chunk="c1", finding_ids=["c1/F1"]
            ),
            rp.ContextNeed(
                path="./foo.h", reason="r2", from_chunk="c1", finding_ids=["c1/F1"]
            ),
            rp.ContextNeed(
                path="`foo.h`", reason="r3", from_chunk="c1", finding_ids=["c1/F1"]
            ),
        ]
        tasks = plan_validation_tasks(store)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].path, "foo.h")
        self.assertEqual(
            [need.path for need in tasks[0].related_needs],
            ["foo.h", "./foo.h", "`foo.h`"],
        )
        self.assertEqual([finding.id for finding in tasks[0].related], ["c1/F1"])

    def test_plan_validation_tasks_keeps_distinct_paths_and_dotfiles(self) -> None:
        store = EvidenceStore()
        store.findings["a"] = _finding("a", severity="MAJOR")
        store.findings["b"] = _finding("b", severity="MAJOR")
        store.findings["c"] = _finding("c", severity="MAJOR")
        store.needs_context = [
            rp.ContextNeed(path="foo.h", reason="root", finding_ids=["a"]),
            rp.ContextNeed(
                path="vendor/lib/foo.h", reason="vendor", finding_ids=["b"]
            ),
            rp.ContextNeed(path=".gitignore", reason="dotfile", finding_ids=["c"]),
        ]
        tasks = plan_validation_tasks(store)
        self.assertEqual(
            [task.path for task in tasks],
            ["foo.h", "vendor/lib/foo.h", ".gitignore"],
        )

    def test_unscheduled_validation_needs_skips_aliases_already_claimed(self) -> None:
        store = EvidenceStore()
        store.findings["c1/F1"] = _finding("c1/F1", severity="BLOCKING")
        store.needs_context = [
            rp.ContextNeed(path="foo.h", reason="r", finding_ids=["c1/F1"]),
        ]
        scheduled: set[str] = set()
        first = rp._unscheduled_validation_needs(store, scheduled)
        self.assertEqual(first, [(0, "foo.h")])
        self.assertEqual(scheduled, {"foo.h"})
        store.needs_context.append(
            rp.ContextNeed(path="./foo.h", reason="r2", finding_ids=["c1/F1"]),
        )
        second = rp._unscheduled_validation_needs(store, scheduled)
        self.assertEqual(second, [])
        self.assertEqual(scheduled, {"foo.h"})

    def test_mark_incomplete_validation_collapses_path_aliases(self) -> None:
        store = EvidenceStore()
        finding = _finding("c1/F1", severity="BLOCKING")
        store.findings[finding.id] = finding
        store.kept.add(finding.id)
        store.reduced = True
        rp._mark_incomplete_validation(store, [finding], "include/missing.h")
        rp._mark_incomplete_validation(store, [finding], "./include/missing.h")
        self.assertEqual(
            finding.evidence, ["validation:incomplete:include/missing.h"]
        )
        self.assertEqual(set(store.incomplete_context), {"include/missing.h"})
        self.assertNotIn("./include/missing.h", store.incomplete_context)
        self.assertNotIn(
            "validation:incomplete:./include/missing.h", finding.evidence
        )
        self.assertTrue(rp._has_incomplete_validation(store))

    def test_adopt_new_needs_does_not_schedule_alias_of_queued_path(self) -> None:
        corpus = self._paths_corpus(["foo.h"])
        findings, needs = self._specs([("F1", "BLOCKING", "LIKELY", "foo.h")])

        def on_validation(_n: int, _user: str, ids: list[str]) -> str:
            return _map_chunks_json(
                ids,
                needs_context=[
                    {
                        "path": "./foo.h",
                        "reason": "same file, different spelling",
                    }
                ],
            )

        fake = self._pipeline(
            findings=findings, needs_context=needs, on_validation=on_validation
        )
        _review, coverage, _store, stats = _run_hierarchical(corpus, fake)
        self.assertTrue(coverage.complete)
        self.assertEqual(stats.validation_requests, 1)
        self.assertEqual(stats.validation_attempts, 1)
        self.assertEqual(len(fake.validation_messages), 1)

    def test_prompt_demotion_copies_do_not_change_rank_views(self) -> None:
        store = EvidenceStore()
        store.findings["confirmed"] = _finding(
            "confirmed", severity="MAJOR", confidence="CONFIRMED"
        )
        store.needs_context = [
            rp.ContextNeed(
                path="include/foo.h",
                reason="still needs context",
                finding_ids=["confirmed"],
            )
        ]
        rank_views = rp.aggregated_related_findings(
            store, [store.findings["confirmed"]]
        )
        self.assertEqual(rank_views[0].confidence, "CONFIRMED")
        prompt = rp.validation_prompt_findings(store, rank_views, 0)
        self.assertEqual(prompt[0].confidence, "LIKELY")
        self.assertEqual(rank_views[0].confidence, "CONFIRMED")
        self.assertEqual(store.findings["confirmed"].confidence, "CONFIRMED")

    def test_independent_validation_calls_overlap(self) -> None:
        paths = ["include/h1.h", "include/h2.h"]
        corpus = self._paths_corpus(paths)
        findings, needs = self._specs(
            [
                ("F1", "MAJOR", "LIKELY", "include/h1.h"),
                ("F2", "MAJOR", "LIKELY", "include/h2.h"),
            ]
        )
        barrier = threading.Barrier(2, timeout=5)
        overlapped = {"ok": False, "error": None}

        def on_validation(_n: int, _user: str, ids: list[str]) -> str:
            try:
                barrier.wait()
                overlapped["ok"] = True
            except threading.BrokenBarrierError as exc:
                overlapped["error"] = exc
            return _map_chunks_json(ids)

        fake = self._pipeline(
            findings=findings, needs_context=needs, on_validation=on_validation
        )
        review, coverage, _store, stats = _run_hierarchical(
            corpus, fake, validation_concurrency=2
        )
        self.assertTrue(coverage.complete)
        self.assertTrue(overlapped["ok"])
        self.assertIsNone(overlapped["error"])
        self.assertEqual(fake.state["validation"], 2)
        self.assertEqual(stats.validation_concurrency, 2)
        self.assertEqual(review["event"], "COMMENT")

    def test_validation_concurrency_never_exceeds_configured_limit(self) -> None:
        paths = [f"include/h{index}.h" for index in range(1, 5)]
        corpus = self._paths_corpus(paths)
        findings, needs = self._specs(
            [
                (f"F{index}", "MAJOR", "LIKELY", path)
                for index, path in enumerate(paths, 1)
            ]
        )
        current = 0
        max_inflight = 0
        lock = threading.Lock()

        def on_validation(_n: int, _user: str, ids: list[str]) -> str:
            nonlocal current, max_inflight
            with lock:
                current += 1
                max_inflight = max(max_inflight, current)
            time.sleep(0.05)
            with lock:
                current -= 1
            return _map_chunks_json(ids)

        fake = self._pipeline(
            findings=findings, needs_context=needs, on_validation=on_validation
        )
        _review, coverage, _store, stats = _run_hierarchical(
            corpus, fake, validation_concurrency=2
        )
        self.assertTrue(coverage.complete)
        self.assertEqual(max_inflight, 2)
        self.assertEqual(stats.validation_attempts, 4)
        self.assertLessEqual(current, 0)

    def test_minor_cannot_consume_final_budget_ahead_of_major(self) -> None:
        paths = ["include/minor.h", "include/major.h"]
        corpus = self._paths_corpus(paths)
        findings, needs = self._specs(
            [
                ("F1", "MINOR", "LIKELY", "include/minor.h"),
                ("F2", "MAJOR", "LIKELY", "include/major.h"),
            ]
        )
        fake = self._pipeline(findings=findings, needs_context=needs)
        with mock.patch.object(rp, "MAX_VALIDATION_CALLS", 1):
            _review, coverage, store, stats = _run_hierarchical(
                corpus, fake, validation_concurrency=2
            )
        self.assertTrue(coverage.complete)
        self.assertEqual(fake.state["validation"], 1)
        self.assertEqual(stats.validation_attempts, 1)
        self.assertEqual(stats.validation_deferred, 1)
        validated = _validation_requested_paths(fake.validation_messages[0])
        self.assertEqual(validated, ["include/major.h"])
        self.assertNotIn(
            "validation:incomplete:include/major.h",
            store.findings[_fid("F2")].evidence,
        )
        self.assertIn(
            "validation:incomplete:include/minor.h",
            store.findings[_fid("F1")].evidence,
        )

    def test_merged_blocking_member_beats_earlier_minor_path(self) -> None:
        paths = ["include/minor.h", "include/critical.h"]
        corpus = self._paths_corpus(paths)
        findings, needs = self._specs(
            [
                ("F1", "MINOR", "LIKELY", "include/minor.h"),
                ("F2", "MINOR", "QUESTION", "include/critical.h"),
                ("F3", "BLOCKING", "LIKELY", "include/critical.h"),
            ]
        )

        def on_reduce(_system: str, user: str) -> str:
            ids = _reduce_payload_ids(user)
            canon = next((item for item in ids if item.endswith("/F2")), None)
            blocking = next((item for item in ids if item.endswith("/F3")), None)
            minor = next((item for item in ids if item.endswith("/F1")), None)
            keep = [item for item in (minor, canon) if item]
            merge = []
            if canon is not None and blocking is not None:
                merge.append({"ids": [canon, blocking], "canonical": canon})
            return json.dumps(
                {"keep": keep or ids, "reject": [], "merge": merge}
            )

        fake = self._pipeline(
            findings=findings, needs_context=needs, on_reduce=on_reduce
        )
        with mock.patch.object(rp, "MAX_VALIDATION_CALLS", 1):
            _review, coverage, store, stats = _run_hierarchical(
                corpus, fake, validation_concurrency=2
            )
        self.assertTrue(coverage.complete)
        self.assertEqual(store.findings[_fid("F2")].severity, "MINOR")
        self.assertEqual(stats.validation_attempts, 1)
        self.assertEqual(fake.state["validation"], 1)
        validated = [
            path
            for message in fake.validation_messages
            for path in _validation_requested_paths(message)
        ]
        self.assertIn("include/critical.h", validated)
        self.assertNotIn("include/minor.h", validated)
        self.assertNotIn(
            "validation:incomplete:include/critical.h",
            store.findings[_fid("F2")].evidence,
        )
        self.assertIn(
            "validation:incomplete:include/minor.h",
            store.findings[_fid("F1")].evidence,
        )
        candidates = _validation_candidate_findings(fake.validation_messages[0])
        self.assertTrue(any(item.get("severity") == "BLOCKING" for item in candidates))

    def test_blocking_is_scheduled_before_major_and_minor(self) -> None:
        paths = ["include/minor.h", "include/major.h", "include/blocking.h"]
        corpus = self._paths_corpus(paths)
        findings, needs = self._specs(
            [
                ("F1", "MINOR", "LIKELY", "include/minor.h"),
                ("F2", "MAJOR", "LIKELY", "include/major.h"),
                ("F3", "BLOCKING", "QUESTION", "include/blocking.h"),
            ]
        )
        order: list[str] = []

        def on_validation(_n: int, user: str, ids: list[str]) -> str:
            order.extend(_validation_requested_paths(user))
            return _map_chunks_json(ids)

        fake = self._pipeline(
            findings=findings, needs_context=needs, on_validation=on_validation
        )
        _run_hierarchical(corpus, fake, validation_concurrency=1)
        self.assertEqual(
            order, ["include/blocking.h", "include/major.h", "include/minor.h"]
        )

    def test_low_value_runs_only_if_budget_remains(self) -> None:
        paths = ["include/minor.h", "include/major.h", "include/blocking.h"]
        corpus = self._paths_corpus(paths)
        findings, needs = self._specs(
            [
                ("F1", "MINOR", "LIKELY", "include/minor.h"),
                ("F2", "MAJOR", "LIKELY", "include/major.h"),
                ("F3", "BLOCKING", "LIKELY", "include/blocking.h"),
            ]
        )
        fake = self._pipeline(findings=findings, needs_context=needs)
        with mock.patch.object(rp, "MAX_VALIDATION_CALLS", 2):
            _review, coverage, store, stats = _run_hierarchical(
                corpus, fake, validation_concurrency=2
            )
        self.assertTrue(coverage.complete)
        validated = [
            path
            for message in fake.validation_messages
            for path in _validation_requested_paths(message)
        ]
        self.assertEqual(
            set(validated), {"include/blocking.h", "include/major.h"}
        )
        self.assertNotIn("include/minor.h", validated)
        self.assertEqual(stats.validation_deferred, 1)
        self.assertIn(
            "validation:incomplete:include/minor.h",
            store.findings[_fid("F1")].evidence,
        )

    def test_workers_do_not_mutate_evidence_store(self) -> None:
        paths = ["include/h1.h", "include/h2.h"]
        corpus = self._paths_corpus([])
        findings, needs = self._specs(
            [
                ("F1", "MAJOR", "LIKELY", "include/h1.h"),
                ("F2", "MAJOR", "LIKELY", "include/h2.h"),
            ]
        )
        main_ident = threading.get_ident()
        ingest_threads: list[int] = []
        load_threads: list[int] = []
        barrier = threading.Barrier(2, timeout=5)
        orig = rp.ingest_map_result

        def wrapped_ingest(*args, **kwargs):
            ingest_threads.append(threading.get_ident())
            return orig(*args, **kwargs)

        def loader(path: str) -> str | None:
            load_threads.append(threading.get_ident())
            return f"/* {path} */\nint field;\n"

        def on_validation(_n: int, _user: str, ids: list[str]) -> str:
            barrier.wait()
            return _map_chunks_json(ids)

        fake = self._pipeline(
            findings=findings, needs_context=needs, on_validation=on_validation
        )
        with mock.patch.object(rp, "ingest_map_result", wrapped_ingest):
            _run_hierarchical(
                corpus,
                fake,
                validation_concurrency=2,
                context_loader=loader,
            )
        self.assertTrue(ingest_threads)
        self.assertTrue(load_threads)
        self.assertTrue(all(ident == main_ident for ident in ingest_threads))
        self.assertTrue(all(ident == main_ident for ident in load_threads))
        self.assertTrue(
            any(ident != main_ident for ident in fake.validation_threads)
        )

    def test_provider_failure_is_isolated_to_one_validation_task(self) -> None:
        paths = ["include/h1.h", "include/h2.h"]
        corpus = self._paths_corpus(paths)
        findings, needs = self._specs(
            [
                ("F1", "MAJOR", "LIKELY", "include/h1.h"),
                ("F2", "MAJOR", "LIKELY", "include/h2.h"),
            ]
        )

        def on_validation(_n: int, user: str, ids: list[str]) -> str:
            if "include/h2.h" in user:
                raise RuntimeError("provider unavailable for sibling path")
            return _map_chunks_json(ids)

        fake = self._pipeline(
            findings=findings, needs_context=needs, on_validation=on_validation
        )
        _review, coverage, store, stats = _run_hierarchical(
            corpus, fake, validation_concurrency=2
        )
        self.assertTrue(coverage.complete)
        self.assertGreaterEqual(stats.validation_attempts, 2)
        self.assertEqual(stats.validation_calls_succeeded, 1)
        self.assertNotIn(
            "validation:incomplete:include/h1.h",
            store.findings[_fid("F1")].evidence,
        )
        self.assertIn(
            "validation:incomplete:include/h2.h",
            store.findings[_fid("F2")].evidence,
        )

    def test_deadline_stops_scheduling_new_validation_work(self) -> None:
        paths = [f"include/h{index}.h" for index in range(1, 7)]
        corpus = self._paths_corpus(paths)
        findings, needs = self._specs(
            [
                (f"F{index}", "MAJOR", "LIKELY", path)
                for index, path in enumerate(paths, 1)
            ]
        )
        calls: list[str] = []
        lock = threading.Lock()

        def on_validation(_n: int, user: str, _ids: list[str]) -> str:
            with lock:
                calls.extend(_validation_requested_paths(user))
            raise PipelineDeadlineExceeded(
                "provider cutoff reached before validation"
            )

        fake = self._pipeline(
            findings=findings, needs_context=needs, on_validation=on_validation
        )
        review, coverage, store, stats = _run_hierarchical(
            corpus, fake, validation_concurrency=2
        )
        self.assertTrue(coverage.complete)
        self.assertEqual(len(calls), 2)
        self.assertEqual(stats.validation_attempts, 2)
        self.assertTrue(stats.validation_deadline_exhausted)
        self.assertFalse(stats.deadline_exhausted)
        self.assertEqual(stats.synthesis_calls, 1)
        self.assertGreaterEqual(stats.validation_deferred, 4)
        self.assertEqual(review["event"], "COMMENT")
        for index, path in enumerate(paths, 1):
            self.assertIn(
                f"validation:incomplete:{path}",
                store.findings[_fid(f"F{index}")].evidence,
            )

    def test_footer_reports_validation_concurrency_and_deferred_work(self) -> None:
        paths = ["include/minor.h", "include/major.h", "include/blocking.h"]
        corpus = self._paths_corpus(paths)
        findings, needs = self._specs(
            [
                ("F1", "MINOR", "LIKELY", "include/minor.h"),
                ("F2", "MAJOR", "LIKELY", "include/major.h"),
                ("F3", "BLOCKING", "LIKELY", "include/blocking.h"),
            ]
        )
        fake = self._pipeline(findings=findings, needs_context=needs)
        with mock.patch.object(rp, "MAX_VALIDATION_CALLS", 1):
            _review, _coverage, _store, stats = _run_hierarchical(
                corpus, fake, validation_concurrency=2
            )
        footer = stats.footer()
        self.assertIn("1 validation call(s)", footer)
        self.assertIn("2 validation worker(s)", footer)
        self.assertIn("2 deferred validation path(s)", footer)

    def test_validation_follow_up_needs_are_scheduled(self) -> None:
        paths = ["include/h1.h", "include/h2.h"]
        corpus = self._paths_corpus(paths)
        findings, needs = self._specs(
            [("F1", "MAJOR", "LIKELY", "include/h1.h")]
        )

        def on_validation(_n: int, user: str, ids: list[str]) -> str:
            extras: dict = {}
            if "include/h1.h" in user:
                extras["needs_context"] = [
                    {
                        "path": "include/h2.h",
                        "reason": "follow-up cross-context check",
                    }
                ]
            return _map_chunks_json(ids, **extras)

        fake = self._pipeline(
            findings=findings, needs_context=needs, on_validation=on_validation
        )
        _review, coverage, store, stats = _run_hierarchical(
            corpus, fake, validation_concurrency=1
        )
        self.assertTrue(coverage.complete)
        validated = [
            path
            for message in fake.validation_messages
            for path in _validation_requested_paths(message)
        ]
        self.assertEqual(validated, ["include/h1.h", "include/h2.h"])
        self.assertEqual(stats.validation_attempts, 2)
        self.assertNotIn(
            "validation:incomplete:include/h2.h",
            store.findings[_fid("F1")].evidence,
        )

    def test_action_yml_exposes_validation_concurrency_contract(self) -> None:
        text = (Path(__file__).resolve().parent / "action.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("validation-concurrency:", text)
        self.assertIn("VALIDATION_CONCURRENCY:", text)
        self.assertIn('--validation-concurrency "${VALIDATION_CONCURRENCY}"', text)
        self.assertRegex(
            text,
            r"validation-concurrency:\n(?:.*\n){0,12}    default: \"2\"",
        )


class GenerateReviewPipelineTests(unittest.TestCase):
    def _generate_args(self, tmp: str) -> argparse.Namespace:
        return argparse.Namespace(
            provider="xai",
            model="grok-4.6",
            prompt_file=str(mw.DEFAULT_PROMPT),
            pr="1",
            head_ref="pr-head",
            output=str(Path(tmp) / "merge-warden.md"),
            json_output=str(Path(tmp) / "merge-warden.json"),
            post=False,
            skip_if_missing_key=False,
        )

    def _run_generate_review(
        self,
        args: argparse.Namespace,
        files: list[dict],
        call_model,
    ) -> int:
        pr = _pr(number=1, title="t", body="b")
        failed_diff = mock.Mock(
            returncode=1,
            stdout="",
            stderr="GraphQL: pull request diff is too large",
        )
        with mock.patch.dict(os.environ, {"XAI_API_KEY": "sk"}):
            with mock.patch.object(mw, "gh_json", return_value=pr):
                with mock.patch.object(mw, "collect_pr_files", return_value=files):
                    with mock.patch.object(mw, "run", return_value=failed_diff):
                        with mock.patch.object(mw, "load_arch_docs", return_value=[]):
                            with mock.patch.object(mw, "call_model", side_effect=call_model):
                                return mw.generate_review(args, "o/r")

    def test_generate_review_failed_diff_reviews_file_patches(self) -> None:
        files = [
            {
                "filename": "secret.c",
                "status": "modified",
                "additions": 1,
                "deletions": 1,
                "patch": "@@ -1,1 +1,1 @@\n-old\n+new secret\n",
            }
        ]
        seen: list[str] = []

        def call_model(_provider, system, user, _model, _key, **_kwargs):
            seen.append(user)
            return _pipeline_model_response(
                system,
                user,
                {"event": "APPROVE", "body": "# APPROVE\nLooks good.\n", "comments": []},
            )

        with tempfile.TemporaryDirectory() as tmp:
            args = self._generate_args(tmp)
            rc = self._run_generate_review(args, files, call_model)
            payload = json.loads(Path(args.json_output).read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        self.assertTrue(seen)
        self.assertIn("new secret", "\n".join(seen))
        self.assertNotIn("failed to load complete diff", "\n".join(seen))
        self.assertEqual(payload["event"], "APPROVE")

    def test_generate_review_failed_diff_without_patches_cannot_approve(self) -> None:
        files = [
            {
                "filename": "secret.c",
                "status": "modified",
                "additions": 50,
                "deletions": 2,
            }
        ]

        def call_model(_provider, system, user, _model, _key, **_kwargs):
            return _pipeline_model_response(
                system,
                user,
                {"event": "APPROVE", "body": "# APPROVE\nLooks good.\n", "comments": []},
            )

        with tempfile.TemporaryDirectory() as tmp:
            args = self._generate_args(tmp)
            rc = self._run_generate_review(args, files, call_model)
            markdown = Path(args.output).read_text(encoding="utf-8")
            payload = json.loads(Path(args.json_output).read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(payload.get("comments") or [], [])
        self.assertIn("did not perform a complete review", markdown)
        self.assertNotEqual(mw.normalize_event(payload["event"], payload["body"]), "APPROVE")
        self.assertNotIn("# APPROVE", markdown)

    def test_generate_review_unparseable_synthesis_writes_comment_json(self) -> None:
        """Prose synthesis must not make generate_review return 1 or skip JSON."""
        files = [
            {
                "filename": "a.c",
                "status": "modified",
                "additions": 1,
                "deletions": 1,
                "patch": "@@ -1,1 +1,1 @@\n-old\n+new\n",
            }
        ]

        def call_model(_provider, system, user, _model, _key, **_kwargs):
            if "merge-warden-map" in system or "merge-warden-reduce" in system:
                return _pipeline_model_response(system, user)
            return "thanks, I will not return JSON"

        with tempfile.TemporaryDirectory() as tmp:
            args = self._generate_args(tmp)
            rc = self._run_generate_review(args, files, call_model)
            json_path = Path(args.json_output)
            self.assertTrue(json_path.is_file())
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(payload.get("comments") or [], [])
        self.assertNotEqual(
            mw.normalize_event(payload["event"], payload["body"]), "APPROVE"
        )

    def test_generate_review_failed_diff_mixed_patches_cannot_approve(self) -> None:
        files = [
            {
                "filename": "small.c",
                "status": "modified",
                "additions": 1,
                "deletions": 1,
                "patch": "@@ -1,1 +1,1 @@\n-old small\n+new small\n",
            },
            {
                "filename": "huge.c",
                "status": "modified",
                "additions": 4000,
                "deletions": 0,
            },
        ]

        def call_model(_provider, system, user, _model, _key, **_kwargs):
            return _pipeline_model_response(
                system,
                user,
                {"event": "APPROVE", "body": "# APPROVE\nLooks good.\n", "comments": []},
            )

        with tempfile.TemporaryDirectory() as tmp:
            args = self._generate_args(tmp)
            rc = self._run_generate_review(args, files, call_model)
            markdown = Path(args.output).read_text(encoding="utf-8")
            payload = json.loads(Path(args.json_output).read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(payload.get("comments") or [], [])
        self.assertIn("did not perform a complete review", markdown)
        self.assertIn("huge.c", markdown)
        self.assertNotEqual(mw.normalize_event(payload["event"], payload["body"]), "APPROVE")
        self.assertNotIn("# APPROVE", markdown)

    def test_generate_review_does_not_approve_when_coverage_fails(self) -> None:
        pr = _pr(number=1, title="t", body="b")
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                provider="xai",
                model="grok-4.6",
                prompt_file=str(mw.DEFAULT_PROMPT),
                pr="1",
                head_ref="pr-head",
                output=str(Path(tmp) / "merge-warden.md"),
                json_output=str(Path(tmp) / "merge-warden.json"),
                post=False,
                skip_if_missing_key=False,
            )
            with mock.patch.dict(os.environ, {"XAI_API_KEY": "sk"}):
                with mock.patch.object(mw, "gh_json", return_value=pr):
                    with mock.patch.object(mw, "collect_pr_files", return_value=[]):
                        with mock.patch.object(
                            mw, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")
                        ):
                            with mock.patch.object(mw, "load_arch_docs", return_value=[]):
                                with mock.patch.object(
                                    mw,
                                    "call_model",
                                    side_effect=RuntimeError("disconnect"),
                                ):
                                    rc = mw.generate_review(args, "o/r")
            markdown = Path(args.output).read_text(encoding="utf-8")
            payload = json.loads(Path(args.json_output).read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(payload.get("comments") or [], [])
        self.assertIn("could not complete a full review", markdown)
        self.assertNotIn("# APPROVE", markdown)


if __name__ == "__main__":
    unittest.main()
