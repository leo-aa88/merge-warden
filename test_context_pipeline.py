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
from context_pipeline import (
    ContextChunk,
    CorpusInputs,
    build_review_corpus,
    chunk_diff,
    chunk_text,
    format_char_count,
    incomplete_coverage_body,
    incomplete_limit_body,
    pack_chunks,
    split_on_headings,
    split_text_by_lines,
)
from review_pipeline import (
    EvidenceStore,
    Finding,
    apply_reduce_decision,
    findings_as_review,
    ingest_map_result,
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
                return json.dumps(
                    {
                        "chunks": [
                            {
                                "chunk_id": corpus.reviewable_chunks[0].id,
                                "findings": [
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
                                "contracts": [{"id": "C1", "text": "owns_string means free"}],
                                "dependencies": [],
                                "needs_context": [],
                            }
                        ]
                    }
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
                return json.dumps(
                    {
                        "chunks": [
                            {
                                "chunk_id": "ignored",
                                "findings": [
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
                                "contracts": [],
                                "dependencies": [],
                                "needs_context": [
                                    {
                                        "path": "stdrot_api.h",
                                        "reason": "Need ownership contract for NativeResult",
                                    }
                                ],
                            }
                        ]
                    }
                )
            if "Context requests" in user:
                stages.append("validation")
                self.assertIn("stdrot_api.h", user)
                self.assertIn("NativeResult", user)
                return json.dumps(
                    {
                        "chunks": [
                            {
                                "chunk_id": "val",
                                "findings": [
                                    {
                                        "id": "F18",
                                        "severity": "BLOCKING",
                                        "path": "src/foo.c",
                                        "line": 1,
                                        "body": "owns_string is ignored before free",
                                        "confidence": "CONFIRMED",
                                    }
                                ],
                                "contracts": [
                                    {"id": "C12", "text": "NativeResult owns strings when owns_string=true"}
                                ],
                                "needs_context": [],
                            }
                        ]
                    }
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
        self.assertTrue(ingest_map_result(store, raw, batch, "M1"))
        self.assertEqual(len(store.findings), 2)

    def test_incomplete_coverage_body_lists_chunks(self) -> None:
        corpus = build_review_corpus(_inputs(diff="+x\n"))
        corpus.coverage.uncovered_chunk_ids = ["D1", "D2", "D3"]
        body = incomplete_coverage_body(corpus.coverage)
        self.assertIn("3 context chunk(s)", body)
        self.assertIn("`D1`", body)
        review = findings_as_review(EvidenceStore(), body)
        self.assertEqual(review["event"], "COMMENT")


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
