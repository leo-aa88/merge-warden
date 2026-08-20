#!/usr/bin/env python3
"""Tests for hierarchical context chunking, packing, and coverage."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import merge_warden as mw
import review_pipeline as rp
from context_pipeline import (
    ContextChunk,
    CorpusInputs,
    ReviewCorpus,
    build_coverage,
    build_review_corpus,
    chunk_diff,
    chunk_text,
    format_char_count,
    incomplete_coverage_body,
    incomplete_limit_body,
    mark_chunks_covered,
    pack_chunks,
    reset_uncovered,
    split_on_headings,
    split_text_by_lines,
)
from review_pipeline import (
    MAX_REDUCE_ROUNDS,
    MAX_VALIDATION_CALLS,
    REDUCE_GROUP_SIZE,
    EvidenceStore,
    Finding,
    PipelineStats,
    apply_reduce_decision,
    findings_as_review,
    format_map_user_message,
    format_validation_user_message,
    hierarchical_reduce,
    ingest_map_result,
    plan_requests,
    run_hierarchical_review,
)


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


def _finding(finding_id: str, body: str | None = None) -> Finding:
    return Finding(
        id=finding_id,
        severity="MAJOR",
        path="a.c",
        side="RIGHT",
        line=1,
        body=body or f"defect {finding_id}",
        confidence="LIKELY",
    )


def _reduce_payload_ids(user: str) -> list[str]:
    data = json.loads(user[user.find("{") :])
    return [item["id"] for item in data["findings"]]


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

    def __call__(self, system: str, user: str) -> str:
        if "merge-warden-map" in system:
            ids = _chunk_ids_in_prompt(user)
            if "Context requests" in user:
                self.validation_messages.append(user)
                return _map_chunks_json(ids)
            self.map_messages.append(user)
            extras: dict = {}
            if not self.map_messages[1:]:
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

    def test_file_contents_are_chunked_not_truncated(self) -> None:
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
        texts = "\n".join(chunk.text for chunk in corpus.chunks if chunk.kind == "file")
        self.assertIn("line-0001-KEEP", texts)
        self.assertIn("line-0200-KEEP", texts)
        self.assertNotIn("truncated", texts)

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
        self.assertEqual(review["event"], "COMMENT")
        self.assertIn("could not complete a full review", review["body"])
        self.assertIn("No approval decision was produced", review["body"])
        self.assertNotIn("# APPROVE", review["body"])

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
                    "comments": [],
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
        self.assertEqual(store.findings["F1"].body, "original evidence body")
        self.assertGreaterEqual(stats.map_calls, 1)
        self.assertEqual(stats.synthesis_calls, 1)

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
        self.assertIn("F18", store.findings)
        self.assertGreaterEqual(stats.validation_calls, 1)
        self.assertEqual(review["event"], "REQUEST_CHANGES")

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

    def test_incomplete_coverage_body_lists_chunks(self) -> None:
        corpus = build_review_corpus(_inputs(diff="+x\n"))
        corpus.coverage.uncovered_chunk_ids = ["D1", "D2", "D3"]
        body = incomplete_coverage_body(corpus.coverage)
        self.assertIn("3 context chunk(s)", body)
        self.assertIn("`D1`", body)
        review = findings_as_review(EvidenceStore(), body)
        self.assertEqual(review["event"], "COMMENT")


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
        self.assertEqual(review["event"], "COMMENT")
        self.assertIn("No approval decision was produced", review["body"])
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
        self.assertEqual(review["event"], "COMMENT")
        self.assertIn("No approval decision was produced", review["body"])

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
        # Round 1: two groups. Round 2: same two groups, then fixed point.
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
        self.assertEqual(review["event"], "COMMENT")
        self.assertIn("No approval decision was produced", review["body"])
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
        corpus = _synthetic_corpus([map_chunk, *matching], index="Changed files:\n- include/foo.h +4 -0\n")
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
        self.assertEqual(stats.validation_chunks, len(matching))
        self.assertEqual(store.findings["F17"].confidence, "LIKELY")
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
        corpus = _synthetic_corpus([map_chunk, *matching])
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
        self.assertIn("validation:incomplete:include/foo.h", store.findings["F17"].evidence)
        self.assertEqual(store.findings["F17"].confidence, "LIKELY")
        self.assertEqual(review["event"], "COMMENT")
        self.assertIn("LIKELY", recorder.synthesis_messages[0])
        self.assertIn("validation:incomplete:include/foo.h", recorder.synthesis_messages[0])

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
        self.assertEqual(review["event"], "COMMENT")
        self.assertIn("No approval decision was produced", review["body"])
        self.assertIn("could not complete a full review", review["body"])


class GenerateReviewPipelineTests(unittest.TestCase):
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
        self.assertIn("could not complete a full review", markdown)
        self.assertNotIn("# APPROVE", markdown)


if __name__ == "__main__":
    unittest.main()
