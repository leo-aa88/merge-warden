#!/usr/bin/env python3
"""Collect, chunk, pack, and track PR context without discarding tails."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
DIFF_GIT_RE = re.compile(r"^diff --git ")
HEADING_RE = re.compile(r"^(#{1,6})\s+\S")
PLUS_FILE_RE = re.compile(r"^\+\+\+ (?:b/)?(.+)$")
MINUS_FILE_RE = re.compile(r"^--- (?:a/)?(.+)$")

DEFAULT_MAX_MAP_REQUEST_CHARS = 225_000
DEFAULT_MAX_REDUCE_REQUEST_CHARS = 225_000
DEFAULT_MAX_SINGLE_CHUNK_CHARS = 100_000
DEFAULT_MAX_TOTAL_REVIEW_CHARS = 10_000_000
DEFAULT_MAX_CONTEXT_CHUNKS = 64
DEFAULT_MAP_OVERHEAD_CHARS = 24_000
DEFAULT_ARCH_COALESCE_CHARS = 80_000
MAX_FAILURE_NOTES_IN_REVIEW = 10


@dataclass
class ContextChunk:
    id: str
    kind: str
    source: str
    text: str
    start_line: int | None = None
    end_line: int | None = None
    excluded: bool = False
    exclusion_reason: str = ""
    member_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.member_ids:
            self.member_ids = [self.id]

    @property
    def size(self) -> int:
        return len(self.text)


@dataclass
class SourceCoverage:
    source: str
    kind: str
    chars: int
    chunks: list[str]
    covered: bool = False
    excluded: bool = False
    exclusion_reason: str = ""
    lines: tuple[int, int] | None = None

    def to_dict(self) -> dict:
        payload = {
            "source": self.source,
            "kind": self.kind,
            "chars": self.chars,
            "chunks": list(self.chunks),
            "covered": self.covered,
            "excluded": self.excluded,
        }
        if self.exclusion_reason:
            payload["exclusion_reason"] = self.exclusion_reason
        if self.lines is not None:
            payload["lines"] = [self.lines[0], self.lines[1]]
        return payload


@dataclass
class CoverageReport:
    sources: list[SourceCoverage]
    uncovered_chunk_ids: list[str] = field(default_factory=list)
    limit_error: str = ""

    @property
    def complete(self) -> bool:
        if self.limit_error or self.uncovered_chunk_ids:
            return False
        return all(item.covered or item.excluded for item in self.sources)

    def to_dict(self) -> dict:
        return {
            "complete": self.complete,
            "limit_error": self.limit_error,
            "uncovered_chunk_ids": list(self.uncovered_chunk_ids),
            "sources": [item.to_dict() for item in self.sources],
        }


@dataclass
class ReviewCorpus:
    chunks: list[ContextChunk]
    coverage: CoverageReport
    index: str
    purpose_summary: str
    total_chars: int
    exclusions: list[str] = field(default_factory=list)
    limit_error: str = ""
    source_chunks: list[ContextChunk] = field(default_factory=list)
    source_chunk_limit: int = DEFAULT_MAX_SINGLE_CHUNK_CHARS

    @property
    def reviewable_chunks(self) -> list[ContextChunk]:
        return [chunk for chunk in self.chunks if not chunk.excluded]

    @property
    def source_context_chunks(self) -> list[ContextChunk]:
        return [chunk for chunk in self.source_chunks if not chunk.excluded]


@dataclass
class CorpusInputs:
    pr: dict
    files: list[dict]
    diff: str
    arch_docs: list[tuple[str, str | None]]
    issues: list[dict]
    omitted_issue_count: int
    file_contents: dict[str, str | None]
    commentable: dict[str, dict[str, set[int]]]
    skipped_paths: set[str]


class ReviewLimitError(RuntimeError):
    """Reviewable context exceeded a hard safety limit."""


def format_char_count(count: int) -> str:
    if count < 10_000:
        return f"{count} characters"
    mb = count / 1_000_000
    if mb >= 1:
        return f"{mb:.1f} MB"
    return f"{count / 1_000:.1f} kB"


def split_text_by_lines(text: str, limit: int) -> list[str]:
    """Split on line boundaries. Only hard-split a line if that line exceeds limit."""
    if limit <= 0:
        return [text] if text else []
    if len(text) <= limit:
        return [text] if text else []

    parts: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        extra = len(line)
        if current and size + extra > limit:
            parts.append("".join(current))
            current = []
            size = 0
        if extra > limit:
            if current:
                parts.append("".join(current))
                current = []
                size = 0
            offset = 0
            while offset < extra:
                parts.append(line[offset : offset + limit])
                offset += limit
            continue
        current.append(line)
        size += extra
    if current:
        parts.append("".join(current))
    return parts or [text]


def _line_span(text: str, start_line: int | None) -> tuple[int | None, int | None]:
    """Inclusive source-line span from newline count.

    Diff chunks must not use this: headers, ``@@``, and ``-`` lines are not
    new-file lines. See ``_diff_line_span``.
    """
    if start_line is None:
        return None, None
    count = text.count("\n")
    if text and not text.endswith("\n"):
        count += 1
    if count <= 0:
        return start_line, start_line
    return start_line, start_line + count - 1


def _make_chunk(
    prefix: str,
    index: int,
    kind: str,
    source: str,
    text: str,
    start_line: int | None = None,
) -> ContextChunk:
    start, end = _line_span(text, start_line)
    chunk_id = f"{prefix}:{index}"
    return ContextChunk(
        id=chunk_id,
        kind=kind,
        source=source,
        text=text,
        start_line=start,
        end_line=end,
        member_ids=[chunk_id],
    )


def _diff_line_span(
    text: str,
    left: int | None = None,
    right: int | None = None,
    resume: bool = False,
) -> tuple[int | None, int | None, int | None, int | None]:
    """File-line span of unified-diff text, plus cursors after the last body line.

    Prefers the new-file (RIGHT) range covered by ``+`` and context lines.
    LEFT-only deletion fragments fall back to old-file lines so the chunk is
    not labeled with a raw newline count. ``left``/``right`` continue a
    mid-hunk split that has no ``@@`` header of its own. ``resume`` skips the
    remainder of a line already counted when ``split_text_by_lines`` hard-split
    it; that tail must not be parsed as a new context line.
    """
    if resume:
        newline = text.find("\n")
        if newline == -1:
            return None, None, left, right
        text = text[newline + 1 :]
    first_right: int | None = None
    last_right: int | None = None
    first_left: int | None = None
    last_left: int | None = None
    in_hunk = left is not None or right is not None
    for raw in text.splitlines():
        if raw.startswith("@@"):
            match = HUNK_RE.match(raw)
            if match:
                left = int(match.group(1))
                right = int(match.group(3))
                in_hunk = True
            continue
        if not in_hunk:
            continue
        if raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            if right is not None:
                if first_right is None:
                    first_right = right
                last_right = right
                right += 1
            continue
        if raw.startswith("-"):
            if left is not None:
                if first_left is None:
                    first_left = left
                last_left = left
                left += 1
            continue
        if right is not None:
            if first_right is None:
                first_right = right
            last_right = right
            right += 1
        if left is not None:
            if first_left is None:
                first_left = left
            last_left = left
            left += 1
    if first_right is not None and last_right is not None:
        return first_right, last_right, left, right
    if first_left is not None and last_left is not None:
        return first_left, last_left, left, right
    return None, None, left, right


def _make_diff_chunk(
    prefix: str,
    index: int,
    source: str,
    text: str,
    left: int | None = None,
    right: int | None = None,
    resume: bool = False,
) -> tuple[ContextChunk, int | None, int | None]:
    start, end, left, right = _diff_line_span(
        text, left=left, right=right, resume=resume
    )
    chunk_id = f"{prefix}:{index}"
    chunk = ContextChunk(
        id=chunk_id,
        kind="diff",
        source=source,
        text=text,
        start_line=start,
        end_line=end,
        member_ids=[chunk_id],
    )
    return chunk, left, right


def chunk_text(
    *,
    prefix: str,
    kind: str,
    source: str,
    text: str,
    limit: int,
    start_line: int | None = 1,
    splitter: str = "lines",
) -> list[ContextChunk]:
    if not text:
        return [_make_chunk(prefix, 1, kind, source, "(empty)\n", start_line)]

    pieces: list[tuple[str, int | None]]
    if splitter == "headings":
        pieces = [
            (section, section_start)
            for section, section_start in split_on_headings(text, start_line or 1)
        ]
    else:
        pieces = [(text, start_line)]

    packed: list[tuple[str, int | None]] = []
    for piece, piece_start in pieces:
        if len(piece) <= limit:
            packed.append((piece, piece_start))
            continue
        offset_line = piece_start
        for part in split_text_by_lines(piece, limit):
            packed.append((part, offset_line))
            if offset_line is not None:
                lines = part.count("\n")
                if part and not part.endswith("\n"):
                    lines += 1
                offset_line += lines

    return [
        _make_chunk(prefix, index, kind, source, part, part_start)
        for index, (part, part_start) in enumerate(packed, 1)
    ]


def split_on_headings(text: str, start_line: int = 1) -> list[tuple[str, int]]:
    lines = text.splitlines(keepends=True)
    if not lines:
        return []
    starts = [0]
    for index, line in enumerate(lines):
        if index == 0:
            continue
        if HEADING_RE.match(line.lstrip("\ufeff")):
            starts.append(index)
    starts.append(len(lines))
    sections: list[tuple[str, int]] = []
    for begin, end in zip(starts, starts[1:]):
        section = "".join(lines[begin:end])
        if section:
            sections.append((section, start_line + begin))
    return sections or [(text, start_line)]


def parse_diff_files(diff: str) -> list[tuple[str, str, list[tuple[str, int | None]]]]:
    """Split a unified diff into (path, header, hunks).

    Each hunk is (hunk_text, new_file_start_line).
    """
    if not (diff or "").strip():
        return []

    lines = diff.splitlines(keepends=True)
    files: list[tuple[int, list[str]]] = []
    current: list[str] | None = None
    for line in lines:
        if DIFF_GIT_RE.match(line):
            current = [line]
            files.append((len(files), current))
            continue
        if current is None:
            current = [line]
            files.append((len(files), current))
            continue
        current.append(line)

    parsed: list[tuple[str, str, list[tuple[str, int | None]]]] = []
    for _, file_lines in files:
        path = _diff_path(file_lines)
        header_lines: list[str] = []
        hunks: list[tuple[str, int | None]] = []
        hunk: list[str] | None = None
        hunk_start: int | None = None
        for line in file_lines:
            match = HUNK_RE.match(line) if line.startswith("@@") else None
            if match:
                if hunk is not None:
                    hunks.append(("".join(hunk), hunk_start))
                hunk = [line]
                hunk_start = int(match.group(3))
                continue
            if hunk is None:
                header_lines.append(line)
            else:
                hunk.append(line)
        if hunk is not None:
            hunks.append(("".join(hunk), hunk_start))
        header = "".join(header_lines)
        if not hunks:
            hunks = [(header or "".join(file_lines), None)]
            header = ""
        parsed.append((path, header, hunks))
    return parsed


def _diff_path(file_lines: list[str]) -> str:
    plus = minus = ""
    for line in file_lines:
        stripped = line.rstrip("\n")
        match = PLUS_FILE_RE.match(stripped)
        if match:
            plus = match.group(1).strip()
            continue
        match = MINUS_FILE_RE.match(stripped)
        if match:
            minus = match.group(1).strip()
    for candidate in (plus, minus):
        if candidate and candidate != "/dev/null":
            return candidate
    if file_lines:
        first = file_lines[0]
        match = re.match(r"^diff --git a/(.+?) b/(.+)$", first.rstrip("\n"))
        if match:
            return match.group(2)
    return "(unknown path)"


def chunk_diff(diff: str, limit: int) -> list[ContextChunk]:
    files = parse_diff_files(diff)
    if not files:
        return [
            ContextChunk(
                id="diff:empty:1",
                kind="diff",
                source="(diff)",
                text="(empty diff)\n",
                member_ids=["diff:empty:1"],
            )
        ]

    chunks: list[ContextChunk] = []
    for path, header, hunks in files:
        prefix = f"diff:{path}"
        index = 1
        pending_parts: list[str] = []
        pending_size = 0

        def flush() -> None:
            nonlocal pending_parts, pending_size, index
            if not pending_parts:
                return
            chunk, _, _ = _make_diff_chunk(prefix, index, path, "".join(pending_parts))
            chunks.append(chunk)
            index += 1
            pending_parts = []
            pending_size = 0

        def emit_oversized(text: str) -> None:
            nonlocal index
            left: int | None = None
            right: int | None = None
            resume = False
            for part in split_text_by_lines(text, limit):
                chunk, left, right = _make_diff_chunk(
                    prefix, index, path, part, left=left, right=right, resume=resume
                )
                chunks.append(chunk)
                index += 1
                resume = bool(part) and not part.endswith("\n")

        for hunk_text, _ in hunks:
            candidate = header + hunk_text if not pending_parts else hunk_text
            if not pending_parts and len(header + hunk_text) > limit:
                emit_oversized(header + hunk_text)
                continue
            if pending_parts and pending_size + len(hunk_text) > limit:
                flush()
                candidate = header + hunk_text
                if len(candidate) > limit:
                    emit_oversized(candidate)
                    continue
            pending_parts.append(candidate)
            pending_size += len(candidate)
        flush()
    return chunks


FAILED_COMPLETE_DIFF_PREFIX = "(failed to load complete diff:"
MISSING_COMPLETE_DIFF_LIMIT = (
    "Complete unified diff could not be loaded and no per-file patch "
    "payloads were available. Merge Warden did not perform a complete review."
)


def failed_complete_diff_placeholder(detail: str) -> str:
    return f"{FAILED_COMPLETE_DIFF_PREFIX} {(detail or '').strip()})\n"


def is_failed_complete_diff(diff: str) -> bool:
    return (diff or "").lstrip().startswith(FAILED_COMPLETE_DIFF_PREFIX)


def _file_patch_text(file_info: dict) -> str:
    patch = file_info.get("patch")
    if not isinstance(patch, str):
        return ""
    return patch


def file_has_usable_patch(file_info: dict) -> bool:
    patch = _file_patch_text(file_info)
    if not patch.strip():
        return False
    return any(HUNK_RE.match(line) for line in patch.splitlines())


def file_requires_complete_patch(
    file_info: dict,
    skipped_paths: set[str] | None = None,
) -> bool:
    """A changed text file must appear in a reconstructed complete diff."""
    path = str(file_info.get("filename") or "").strip()
    if not path:
        return False
    if skipped_paths and path in skipped_paths:
        return False
    additions = file_info.get("additions") or 0
    deletions = file_info.get("deletions") or 0
    return bool(additions) or bool(deletions)


def omitted_required_patch_paths(
    files: Iterable[dict],
    skipped_paths: set[str] | None = None,
) -> list[str]:
    omitted: list[str] = []
    for file_info in files:
        if not file_requires_complete_patch(file_info, skipped_paths):
            continue
        if not file_has_usable_patch(file_info):
            omitted.append(str(file_info.get("filename") or "").strip())
    return omitted


def missing_complete_diff_limit(
    files: Iterable[dict],
    skipped_paths: set[str] | None = None,
) -> str:
    omitted = omitted_required_patch_paths(files, skipped_paths)
    if not omitted:
        return MISSING_COMPLETE_DIFF_LIMIT
    listed = ", ".join(f"`{path}`" for path in omitted)
    return (
        "Complete unified diff could not be loaded and per-file patch "
        f"payloads omitted changed files: {listed}. "
        "Merge Warden did not perform a complete review."
    )


def unified_diff_from_file_patches(files: Iterable[dict]) -> str:
    """Rebuild a reviewable unified diff from Pulls Files API `patch` fields."""
    parts: list[str] = []
    for file_info in files:
        path = str(file_info.get("filename") or "").strip()
        patch = _file_patch_text(file_info)
        if not path or not file_has_usable_patch(file_info):
            continue
        body = patch if patch.endswith("\n") else patch + "\n"
        if DIFF_GIT_RE.match(body):
            parts.append(body)
            continue
        status = str(file_info.get("status") or "modified")
        previous = str(file_info.get("previous_filename") or "").strip()
        old_path = previous or path
        if status == "added":
            header = (
                f"diff --git a/{path} b/{path}\n"
                f"--- /dev/null\n"
                f"+++ b/{path}\n"
            )
        elif status == "removed":
            header = (
                f"diff --git a/{old_path} b/{old_path}\n"
                f"--- a/{old_path}\n"
                f"+++ /dev/null\n"
            )
        else:
            header = (
                f"diff --git a/{old_path} b/{path}\n"
                f"--- a/{old_path}\n"
                f"+++ b/{path}\n"
            )
        parts.append(header + body)
    return "".join(parts)


def resolve_reviewable_diff(
    diff: str,
    files: Iterable[dict],
    skipped_paths: set[str] | None = None,
) -> tuple[str, bool]:
    """Return (reconstructed_or_empty, missing_complete) for corpus construction.

    A successful complete unified diff is used as-is. A failed placeholder is
    never reviewable source: reconstruct hunks from the file inventory, but
    decide completeness from that inventory rather than from a nonempty
    reconstruction. Materialize ``files`` once so a generator cannot be
    consumed before the completeness scan.
    """
    if not is_failed_complete_diff(diff):
        return diff, False
    file_list = list(files)
    reconstructed = unified_diff_from_file_patches(file_list)
    omitted = omitted_required_patch_paths(file_list, skipped_paths)
    missing_complete = bool(omitted) or not reconstructed
    return reconstructed, missing_complete


def chunk_source_file(path: str, text: str, limit: int) -> list[ContextChunk]:
    numbered = number_lines(text)
    return chunk_text(
        prefix=f"file:{path}",
        kind="file",
        source=path,
        text=numbered,
        limit=limit,
        start_line=1,
        splitter="lines",
    )


def number_lines(text: str) -> str:
    lines = text.splitlines()
    width = max(len(str(len(lines))), 1)
    return "\n".join(f"{index:>{width}}| {line}" for index, line in enumerate(lines, 1))


def compact_ranges(lines: set[int]) -> str:
    if not lines:
        return "(none)"
    ordered = sorted(lines)
    ranges: list[str] = []
    start = prev = ordered[0]
    for value in ordered[1:]:
        if value == prev + 1:
            prev = value
            continue
        ranges.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = value
    ranges.append(f"{start}-{prev}" if start != prev else str(start))
    return ", ".join(ranges)


def commentable_note(path: str, commentable: dict[str, dict[str, set[int]]]) -> str:
    sides = commentable.get(path) or {}
    right = compact_ranges(sides.get("RIGHT") or set())
    left = compact_ranges(sides.get("LEFT") or set())
    return f"Commentable `{path}` RIGHT: {right}; LEFT: {left}\n"


def pack_chunks(chunks: list[ContextChunk], limit: int) -> list[list[ContextChunk]]:
    batches: list[list[ContextChunk]] = []
    current: list[ContextChunk] = []
    size = 0
    for chunk in chunks:
        extra = len(chunk.text)
        if current and size + extra > limit:
            batches.append(current)
            current = []
            size = 0
        current.append(chunk)
        size += extra
    if current:
        batches.append(current)
    return batches


def merge_chunk_pair(left: ContextChunk, right: ContextChunk, new_id: str) -> ContextChunk:
    start = left.start_line
    end = right.end_line if right.end_line is not None else left.end_line
    if start is not None and right.start_line is not None:
        start = min(start, right.start_line)
    if left.end_line is not None and right.end_line is not None:
        end = max(left.end_line, right.end_line)
    kind = left.kind if left.kind == right.kind else "mixed"
    source = left.source if left.source == right.source else f"{left.source}|{right.source}"
    text = left.text.rstrip() + "\n\n---\n\n" + right.text.lstrip()
    return ContextChunk(
        id=new_id,
        kind=kind,
        source=source,
        text=text,
        start_line=start,
        end_line=end,
        member_ids=[*left.member_ids, *right.member_ids],
    )


def coalesce_same_source(
    chunks: list[ContextChunk],
    limit: int,
    *,
    kinds: set[str] | None = None,
    skip_kinds: set[str] | None = None,
    id_prefix: str = "coalesce",
) -> list[ContextChunk]:
    """Merge adjacent same-source chunks up to ``limit``.

    Original section identities are preserved on ``member_ids``. ``kinds``
    restricts which chunk kinds may merge; ``skip_kinds`` excludes kinds.
    Neither argument silently drops content.
    """
    if not chunks:
        return []
    merged: list[ContextChunk] = []
    current = chunks[0]
    serial = 1
    for chunk in chunks[1:]:
        eligible = chunk.kind == current.kind and chunk.source == current.source
        if kinds is not None:
            eligible = eligible and current.kind in kinds and chunk.kind in kinds
        if skip_kinds is not None:
            eligible = eligible and current.kind not in skip_kinds and chunk.kind not in skip_kinds
        combined = current.size + chunk.size + 8
        if eligible and combined <= limit:
            current = merge_chunk_pair(current, chunk, f"{id_prefix}:{serial}")
            serial += 1
            continue
        merged.append(current)
        current = chunk
    merged.append(current)
    return merged


def fit_chunk_count(
    chunks: list[ContextChunk],
    max_chunks: int,
    max_chunk_chars: int,
) -> list[ContextChunk]:
    fitted = list(chunks)
    serial = 1
    while len(fitted) > max_chunks:
        best_index = -1
        best_size = max_chunk_chars + 1
        for index in range(len(fitted) - 1):
            combined = fitted[index].size + fitted[index + 1].size + 8
            if combined <= max_chunk_chars and combined < best_size:
                best_size = combined
                best_index = index
        if best_index < 0:
            break
        paired = merge_chunk_pair(
            fitted[best_index],
            fitted[best_index + 1],
            f"merged:{serial}",
        )
        serial += 1
        fitted[best_index : best_index + 2] = [paired]
    return fitted


def format_changed_files_index(files: list[dict]) -> str:
    if not files:
        return "Changed files:\n- (none)\n"
    lines = ["Changed files:"]
    for file_info in files:
        path = file_info.get("filename") or "(unknown)"
        added = file_info.get("additions", 0)
        deleted = file_info.get("deletions", 0)
        status = file_info.get("status") or "modified"
        extra = f" ({status})" if status not in {"modified", "changed"} else ""
        lines.append(f"- {path}  +{added} -{deleted}{extra}")
    return "\n".join(lines) + "\n"


def compact_purpose_summary(pr: dict, arch_docs: list[tuple[str, str | None]]) -> str:
    title = str(pr.get("title") or "").strip() or "(no title)"
    body = str(pr.get("body") or "").strip() or "(empty PR description)"
    body_preview = body if len(body) <= 2000 else body[:2000].rstrip() + "\n[preview only; full PR body is in context chunks]\n"
    parts = [f"Title: {title}", "", body_preview]
    for path, text in arch_docs:
        if not text:
            continue
        preview = text if len(text) <= 1500 else text[:1500].rstrip() + "\n"
        parts.append(f"Arch preview `{path}`:\n{preview}")
        break
    return "\n".join(parts).rstrip() + "\n"


def format_pr_index(pr: dict, files: list[dict], exclusions: Iterable[str]) -> str:
    labels = ", ".join(
        label.get("name", "") for label in pr.get("labels") or [] if label.get("name")
    )
    author = (pr.get("author") or {}).get("login") or "unknown"
    lines = [
        f"PR #{pr.get('number')}",
        f"URL: {pr.get('url')}",
        f"Title: {pr.get('title')}",
        f"Author: {author}",
        f"base: {pr.get('baseRefName')}  head: {pr.get('headRefName')} (`{pr.get('headRefOid')}`)",
        f"Labels: {labels or '(none)'}",
        "",
        format_changed_files_index(files).rstrip(),
    ]
    exclusion_list = [item for item in exclusions if item]
    if exclusion_list:
        lines.append("")
        lines.append("Explicitly excluded from content review:")
        lines.extend(f"- {item}" for item in exclusion_list)
    return "\n".join(lines) + "\n"


def _source_key(kind: str, source: str) -> str:
    return f"{kind}:{source}"


def build_coverage(chunks: list[ContextChunk]) -> CoverageReport:
    grouped: dict[str, SourceCoverage] = {}
    order: list[str] = []
    for chunk in chunks:
        key = _source_key(chunk.kind, chunk.source)
        if key not in grouped:
            grouped[key] = SourceCoverage(
                source=chunk.source,
                kind=chunk.kind,
                chars=0,
                chunks=[],
                covered=False,
                excluded=chunk.excluded,
                exclusion_reason=chunk.exclusion_reason,
                lines=None,
            )
            order.append(key)
        item = grouped[key]
        item.chars += chunk.size
        item.chunks.extend(chunk.member_ids)
        item.excluded = item.excluded and chunk.excluded
        if chunk.exclusion_reason and not item.exclusion_reason:
            item.exclusion_reason = chunk.exclusion_reason
        if chunk.start_line is not None:
            start = chunk.start_line
            end = chunk.end_line if chunk.end_line is not None else chunk.start_line
            if item.lines is None:
                item.lines = (start, end)
            else:
                item.lines = (min(item.lines[0], start), max(item.lines[1], end))
    return CoverageReport(sources=[grouped[key] for key in order])


def all_reviewable_context_covered(report: CoverageReport) -> bool:
    return report.complete


def mark_chunks_covered(report: CoverageReport, chunks: Iterable[ContextChunk]) -> None:
    covered: set[str] = set()
    for chunk in chunks:
        covered.update(chunk.member_ids)
        covered.add(chunk.id)
    report.uncovered_chunk_ids = [
        chunk_id for chunk_id in report.uncovered_chunk_ids if chunk_id not in covered
    ]
    for source in report.sources:
        if source.excluded:
            source.covered = True
            continue
        source.covered = bool(source.chunks) and all(
            chunk_id in covered for chunk_id in source.chunks
        )


def reset_uncovered(report: CoverageReport, chunks: Iterable[ContextChunk]) -> None:
    ids: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        if chunk.excluded:
            continue
        for chunk_id in chunk.member_ids:
            if chunk_id not in seen:
                seen.add(chunk_id)
                ids.append(chunk_id)
    report.uncovered_chunk_ids = ids
    for source in report.sources:
        source.covered = bool(source.excluded)


def chunks_matching_path(chunks: list[ContextChunk], path: str) -> list[ContextChunk]:
    needle = (path or "").strip().strip("`")
    if not needle:
        return []
    matches: list[ContextChunk] = []
    for chunk in chunks:
        source = chunk.source
        if source == needle or source.endswith("/" + needle) or needle in source.split("|"):
            matches.append(chunk)
            continue
        if needle == source.split("/")[-1]:
            matches.append(chunk)
    return matches


def build_review_corpus(
    inputs: CorpusInputs,
    *,
    max_single_chunk_chars: int = DEFAULT_MAX_SINGLE_CHUNK_CHARS,
    max_total_review_chars: int = DEFAULT_MAX_TOTAL_REVIEW_CHARS,
    max_context_chunks: int = DEFAULT_MAX_CONTEXT_CHUNKS,
) -> ReviewCorpus:
    chunks: list[ContextChunk] = []
    source_chunks: list[ContextChunk] = []
    exclusions: list[str] = []
    pr = inputs.pr
    metadata = "\n".join(
        [
            f"- URL: {pr.get('url')}",
            f"- Title: {pr.get('title')}",
            f"- Author: {(pr.get('author') or {}).get('login') or 'unknown'}",
            f"- Base: {pr.get('baseRefName')} <- head: {pr.get('headRefName')} "
            f"(`{pr.get('headRefOid')}`)",
        ]
    )
    chunks.extend(
        chunk_text(
            prefix="metadata:pr",
            kind="metadata",
            source="pr-metadata",
            text=metadata + "\n",
            limit=max_single_chunk_chars,
            start_line=1,
        )
    )
    body = pr.get("body") or "(empty PR description)"
    chunks.extend(
        chunk_text(
            prefix="pr_body:description",
            kind="pr_body",
            source="PR description",
            text=str(body),
            limit=max_single_chunk_chars,
            start_line=1,
            splitter="headings",
        )
    )

    for path, text in inputs.arch_docs:
        if text is None:
            exclusions.append(f"{path} (not present on the default branch)")
            chunks.append(
                ContextChunk(
                    id=f"arch:{path}:missing",
                    kind="arch",
                    source=path,
                    text=f"`{path}` is not present on the default branch.\n",
                    excluded=True,
                    exclusion_reason="missing on default branch",
                    member_ids=[f"arch:{path}:missing"],
                )
            )
            continue
        chunks.extend(
            chunk_text(
                prefix=f"arch:{path}",
                kind="arch",
                source=path,
                text=text,
                limit=max_single_chunk_chars,
                start_line=1,
                splitter="headings",
            )
        )

    if not inputs.issues:
        chunks.append(
            ContextChunk(
                id="issue:none:1",
                kind="issue",
                source="(no linked issues)",
                text="(no linked issues found)\n",
                member_ids=["issue:none:1"],
            )
        )
    for issue in inputs.issues:
        number = issue.get("number")
        source = f"issue#{number}"
        labels = ", ".join(
            label.get("name", "") for label in issue.get("labels") or [] if label.get("name")
        )
        text = (
            f"### Issue #{number}: {issue.get('title') or ''}\n\n"
            f"State: {issue.get('state')}\n"
            f"Labels: {labels or '(none)'}\n\n"
            f"{issue.get('body') or '(empty issue body)'}\n"
        )
        if issue.get("error"):
            text = f"### Issue #{number}\n\nCould not load issue: {issue['error']}\n"
        chunks.extend(
            chunk_text(
                prefix=f"issue:{number}",
                kind="issue",
                source=source,
                text=text,
                limit=max_single_chunk_chars,
                start_line=1,
                splitter="headings",
            )
        )
    if inputs.omitted_issue_count:
        reason = (
            f"{inputs.omitted_issue_count} additional linked issue(s) omitted; "
            "capped at the configured linked-issue fanout"
        )
        exclusions.append(reason)
        chunks.append(
            ContextChunk(
                id="issue:omitted:1",
                kind="issue",
                source="(omitted linked issues)",
                text=reason + ".\n",
                excluded=True,
                exclusion_reason=reason,
                member_ids=["issue:omitted:1"],
            )
        )

    file_list = list(inputs.files)
    diff, missing_complete_diff = resolve_reviewable_diff(
        inputs.diff,
        file_list,
        skipped_paths=inputs.skipped_paths,
    )
    if diff:
        chunks.extend(chunk_diff(diff, max_single_chunk_chars))
    if missing_complete_diff:
        exclusions.append("complete unified diff unavailable")
        if not diff:
            chunks.append(
                ContextChunk(
                    id="diff:unavailable:1",
                    kind="diff",
                    source="(unavailable complete diff)",
                    text="(complete unified diff was not available)\n",
                    excluded=True,
                    exclusion_reason="complete unified diff unavailable",
                    member_ids=["diff:unavailable:1"],
                )
            )

    for file_info in inputs.files:
        path = file_info.get("filename") or ""
        if not path:
            continue
        status = file_info.get("status") or "modified"
        header = f"`{path}` ({status})"
        previous = file_info.get("previous_filename")
        if previous:
            header += f" (from `{previous}`)"
        note = commentable_note(path, inputs.commentable)
        if status == "removed":
            chunks.append(
                ContextChunk(
                    id=f"file:{path}:deleted",
                    kind="file",
                    source=path,
                    text=f"{header}\n{note}\n(file deleted in this PR)\n",
                    member_ids=[f"file:{path}:deleted"],
                )
            )
            continue
        if path in inputs.skipped_paths:
            reason = "binary or generated file"
            exclusions.append(f"{path} ({reason})")
            chunks.append(
                ContextChunk(
                    id=f"file:{path}:excluded",
                    kind="file",
                    source=path,
                    text=f"{header}\n{note}\n(explicitly excluded: {reason})\n",
                    excluded=True,
                    exclusion_reason=reason,
                    member_ids=[f"file:{path}:excluded"],
                )
            )
            continue
        if path not in inputs.file_contents:
            continue
        content = inputs.file_contents[path]
        if content is None:
            continue
        file_chunks = chunk_source_file(path, content, max_single_chunk_chars)
        if file_chunks:
            file_chunks[0].text = f"{header}\n{note}\n" + file_chunks[0].text
        source_chunks.extend(file_chunks)

    arch_limit = min(max_single_chunk_chars, DEFAULT_ARCH_COALESCE_CHARS)
    chunks = coalesce_same_source(
        chunks,
        arch_limit,
        kinds={"arch"},
        id_prefix="coalesce:arch",
    )
    chunks = coalesce_same_source(
        chunks,
        max_single_chunk_chars,
        skip_kinds={"arch"},
    )
    excluded_chunks = [chunk for chunk in chunks if chunk.excluded]
    reviewable = [chunk for chunk in chunks if not chunk.excluded]
    reviewable = fit_chunk_count(reviewable, max_context_chunks, max_single_chunk_chars)
    chunks = excluded_chunks + reviewable

    total_chars = sum(chunk.size for chunk in chunks if not chunk.excluded)
    limit_error = ""
    if missing_complete_diff:
        limit_error = missing_complete_diff_limit(file_list, inputs.skipped_paths)
    elif total_chars > max_total_review_chars:
        limit_error = (
            f"PR contains {format_char_count(total_chars)} of reviewable context. "
            f"Configured limit is {format_char_count(max_total_review_chars)}. "
            "Merge Warden did not perform a complete review."
        )
    elif len([chunk for chunk in chunks if not chunk.excluded]) > max_context_chunks:
        reviewable = len([chunk for chunk in chunks if not chunk.excluded])
        limit_error = (
            f"PR produced {reviewable} reviewable context chunks. "
            f"Configured limit is {max_context_chunks}. "
            "Merge Warden did not perform a complete review."
        )

    coverage = build_coverage(chunks)
    coverage.limit_error = limit_error
    reset_uncovered(coverage, chunks)
    index = format_pr_index(pr, inputs.files, exclusions)
    purpose = compact_purpose_summary(pr, inputs.arch_docs)
    return ReviewCorpus(
        chunks=chunks,
        coverage=coverage,
        index=index,
        purpose_summary=purpose,
        total_chars=total_chars,
        exclusions=exclusions,
        limit_error=limit_error,
        source_chunks=source_chunks,
        source_chunk_limit=max_single_chunk_chars,
    )


def format_chunk_for_prompt(chunk: ContextChunk) -> str:
    loc = ""
    if chunk.start_line is not None:
        end = chunk.end_line if chunk.end_line is not None else chunk.start_line
        loc = f" lines={chunk.start_line}-{end}"
    excluded = " excluded=true" if chunk.excluded else ""
    return (
        f"## CHUNK id={chunk.id} kind={chunk.kind} source={chunk.source}{loc}{excluded}\n"
        f"```\n{chunk.text.rstrip()}\n```\n"
    )


def incomplete_limit_body(message: str) -> str:
    return (
        "# COMMENT\n\n"
        f"{message.rstrip()}\n\n"
        "No approval decision was produced.\n"
    )


def incomplete_coverage_body(
    report: CoverageReport,
    *,
    analyzed: int | None = None,
    total: int | None = None,
    failure_notes: list[str] | None = None,
) -> str:
    uncovered = report.uncovered_chunk_ids
    details = "\n".join(f"- `{chunk_id}`" for chunk_id in uncovered[:40])
    extra = ""
    if len(uncovered) > 40:
        extra = f"\n- … {len(uncovered) - 40} more\n"
    sections = [
        "# COMMENT",
        "",
        (
            "Merge Warden could not complete a full review because "
            f"{len(uncovered)} context chunk(s) were not analyzed."
        ),
        "",
    ]
    if analyzed is not None and total is not None:
        sections.extend(
            [
                f"Coverage: {analyzed} / {total} context chunks analyzed.",
                "",
            ]
        )
    if details or extra:
        sections.append(details + extra)
        if not extra:
            sections.append("")
    notes = [note for note in (failure_notes or []) if note]
    if notes:
        shown = notes[:MAX_FAILURE_NOTES_IN_REVIEW]
        remainder = len(notes) - len(shown)
        sections.append("Map failures:")
        sections.extend(f"- {note}" for note in shown)
        if remainder > 0:
            sections.append(f"- … {remainder} more")
        sections.append("")
    sections.append("No approval decision was produced.")
    return "\n".join(sections).rstrip() + "\n"
