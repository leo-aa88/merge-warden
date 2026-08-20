#!/usr/bin/env python3
"""Merge Warden: collect PR context, call Grok, post a GitHub review."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

MARKER = "<!-- merge-warden -->"
XAI_URL = "https://api.x.ai/v1/chat/completions"
DEFAULT_MODEL = "grok-4.6"
MAX_REVIEW_CHARS = 60000
MAX_USER_CHARS = 1_200_000
MAX_FILE_CHARS = 120_000
MAX_DIFF_CHARS = 250_000
MAX_DOC_CHARS = 80_000
MAX_COMMENTS = 25
MAX_COMMENT_CHARS = 8000
BOT_LOGINS = {"github-actions[bot]"}
DEFAULT_PROMPT = Path(__file__).resolve().parent / "prompt.md"
DEFAULT_ARCH_CANDIDATES = (
    "AGENTS.md",
    "ARCHITECTURE.md",
    "CONTRIBUTING.md",
    "README.md",
    "docs/ARCHITECTURE.md",
    "docs/architecture.md",
)

SKIP_SUFFIXES = (
    ".wasm",
    ".so",
    ".o",
    ".a",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
)
DEFAULT_SKIP_NAMES = {
    "lang.tab.c",
    "lang.tab.h",
    "lex.yy.c",
}

ISSUE_REF_RE = re.compile(
    r"(?:(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+)?#(\d+)",
    re.IGNORECASE,
)
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class CommandError(RuntimeError):
    pass


def run(
    args: list[str],
    *,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, text=True, capture_output=True, input=input_text)
    if check and result.returncode != 0:
        stderr = result.stderr.strip()
        raise CommandError(f"{' '.join(args)} failed: {stderr or result.stdout.strip()}")
    return result


def gh_json(args: list[str]):
    result = run(["gh", *args])
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def gh_api(method: str, path: str, payload: dict | None = None, paginate: bool = False):
    args = ["gh", "api", "--method", method, path]
    if paginate:
        args.insert(2, "--paginate")
    if payload is None:
        result = run(args)
        raw = result.stdout.strip()
        return json.loads(raw) if raw else None
    result = run(args + ["--input", "-"], input_text=json.dumps(payload))
    raw = result.stdout.strip()
    return json.loads(raw) if raw else None


def gh_api_paginate_items(path: str) -> list[dict]:
    result = run(["gh", "api", "--paginate", path, "--jq", ".[]"], check=False)
    if result.returncode != 0:
        raise CommandError(result.stderr.strip() or f"failed to paginate {path}")
    items: list[dict] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


def truncate(text: str, limit: int, label: str) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit] + f"\n\n[truncated {label}: {omitted} characters omitted]\n"


def parse_path_list(raw: str) -> list[str]:
    items: list[str] = []
    for chunk in (raw or "").replace(",", "\n").splitlines():
        path = chunk.strip()
        if path:
            items.append(path)
    return items


def load_arch_docs() -> list[str]:
    specified = parse_path_list(os.environ.get("ARCH_DOCS", ""))
    if specified:
        return specified
    return [path for path in DEFAULT_ARCH_CANDIDATES if Path(path).is_file()]


def load_skip_names() -> set[str]:
    names = set(DEFAULT_SKIP_NAMES)
    names.update(parse_path_list(os.environ.get("SKIP_NAMES", "")))
    return names


def is_skipped_path(path: str, skip_names: set[str] | None = None) -> bool:
    names = skip_names if skip_names is not None else load_skip_names()
    name = Path(path).name
    if name in names:
        return True
    lower = path.lower()
    return any(lower.endswith(suffix) for suffix in SKIP_SUFFIXES)


def git_show(ref: str, path: str) -> str | None:
    result = run(["git", "show", f"{ref}:{path}"], check=False)
    if result.returncode != 0:
        return None
    if "\0" in result.stdout:
        return None
    return result.stdout


def number_lines(text: str) -> str:
    lines = text.splitlines()
    width = max(len(str(len(lines))), 1)
    return "\n".join(f"{index:>{width}}| {line}" for index, line in enumerate(lines, 1))


def parse_patch(patch: str) -> dict[str, set[int]]:
    right: set[int] = set()
    left: set[int] = set()
    old_line = 0
    new_line = 0
    in_hunk = False
    for raw in (patch or "").splitlines():
        if raw.startswith("@@"):
            match = HUNK_RE.match(raw)
            if not match:
                continue
            old_line = int(match.group(1))
            new_line = int(match.group(3))
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            right.add(new_line)
            new_line += 1
        elif raw.startswith("-"):
            left.add(old_line)
            old_line += 1
        else:
            left.add(old_line)
            right.add(new_line)
            old_line += 1
            new_line += 1
    return {"RIGHT": right, "LEFT": left}


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


def commentable_by_path(files: list[dict]) -> dict[str, dict[str, set[int]]]:
    mapping: dict[str, dict[str, set[int]]] = {}
    for file_info in files:
        path = file_info.get("filename") or ""
        if not path:
            continue
        mapping[path] = parse_patch(file_info.get("patch") or "")
    return mapping


def format_commentable_lines(commentable: dict[str, dict[str, set[int]]]) -> str:
    if not commentable:
        return "(no commentable diff lines)\n"
    parts = [
        "Only these path/side/line triples can be posted as GitHub inline comments."
    ]
    for path, sides in commentable.items():
        right = compact_ranges(sides.get("RIGHT") or set())
        left = compact_ranges(sides.get("LEFT") or set())
        parts.append(f"- `{path}` RIGHT: {right}")
        parts.append(f"  LEFT: {left}")
    return "\n".join(parts) + "\n"


def nearest_line(lines: set[int], target: int) -> int | None:
    if not lines:
        return None
    if target in lines:
        return target
    return min(lines, key=lambda value: (abs(value - target), value))


def first_commentable(
    commentable: dict[str, dict[str, set[int]]],
) -> tuple[str, str, int] | None:
    for path, sides in commentable.items():
        right = sides.get("RIGHT") or set()
        if right:
            return path, "RIGHT", min(right)
        left = sides.get("LEFT") or set()
        if left:
            return path, "LEFT", min(left)
    return None


def snap_comment(
    comment: dict,
    commentable: dict[str, dict[str, set[int]]],
) -> dict | None:
    path = str(comment.get("path") or "").strip()
    side = str(comment.get("side") or "RIGHT").upper()
    if side not in {"LEFT", "RIGHT"}:
        side = "RIGHT"
    try:
        line = int(comment.get("line"))
    except (TypeError, ValueError):
        line = 1
    if path not in commentable:
        fallback = first_commentable(commentable)
        if fallback is None:
            return None
        path, side, line = fallback
    sides = commentable[path]
    snapped = nearest_line(sides.get(side) or set(), line)
    if snapped is None:
        other = "LEFT" if side == "RIGHT" else "RIGHT"
        snapped = nearest_line(sides.get(other) or set(), line)
        side = other if snapped is not None else side
    if snapped is None:
        fallback = first_commentable(commentable)
        if fallback is None:
            return None
        path, side, snapped = fallback
    return {"path": path, "side": side, "line": snapped}


def collect_issue_bodies(repo: str, pr_body: str, closing: list[dict]) -> str:
    numbers: list[int] = []
    for item in closing:
        number = item.get("number")
        if isinstance(number, int):
            numbers.append(number)
    for match in ISSUE_REF_RE.finditer(pr_body or ""):
        numbers.append(int(match.group(1)))

    seen: set[int] = set()
    unique: list[int] = []
    for number in numbers:
        if number not in seen:
            seen.add(number)
            unique.append(number)

    if not unique:
        return "(no linked issues found)\n"

    sections: list[str] = []
    for number in unique:
        try:
            issue = gh_json(
                [
                    "issue",
                    "view",
                    str(number),
                    "--repo",
                    repo,
                    "--json",
                    "number,title,body,state,labels",
                ]
            )
        except CommandError as exc:
            sections.append(f"### Issue #{number}\n\nCould not load issue: {exc}\n")
            continue
        labels = ", ".join(
            label.get("name", "") for label in issue.get("labels") or [] if label.get("name")
        )
        body = issue.get("body") or "(empty issue body)"
        sections.append(
            f"### Issue #{issue['number']}: {issue.get('title') or ''}\n\n"
            f"State: {issue.get('state')}\n"
            f"Labels: {labels or '(none)'}\n\n"
            f"{body}\n"
        )
    return "\n".join(sections)


def collect_pr_files(repo: str, pr_number: str) -> list[dict]:
    result = run(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/pulls/{pr_number}/files",
            "--jq",
            ".[]",
        ]
    )
    files: list[dict] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            files.append(json.loads(line))
    return files


def collect_changed_files(head_ref: str, files: list[dict]) -> str:
    sections: list[str] = []
    for file_info in files:
        path = file_info.get("filename") or ""
        status = file_info.get("status") or "modified"
        previous = file_info.get("previous_filename")
        header = f"### `{path}` ({status})"
        if previous:
            header += f" (from `{previous}`)"
        if status == "removed":
            sections.append(f"{header}\n\n(file deleted in this PR)\n")
            continue
        if is_skipped_path(path):
            sections.append(f"{header}\n\n(skipped binary or generated file)\n")
            continue
        content = git_show(head_ref, path)
        if content is None:
            sections.append(f"{header}\n\n(contents unavailable at {head_ref})\n")
            continue
        numbered = number_lines(truncate(content, MAX_FILE_CHARS, path))
        sections.append(f"{header}\n\n```\n{numbered}\n```\n")
    return "\n".join(sections) if sections else "(no changed files)\n"


def collect_arch_docs() -> str:
    sections: list[str] = []
    docs = load_arch_docs()
    if not docs:
        return "(no architectural docs provided)\n"
    for path in docs:
        file_path = Path(path)
        if not file_path.is_file():
            sections.append(f"### `{path}`\n\n(not present on the default branch)\n")
            continue
        text = file_path.read_text(encoding="utf-8", errors="replace")
        sections.append(
            f"### `{path}`\n\n```markdown\n{truncate(text, MAX_DOC_CHARS, path)}\n```\n"
        )
    return "\n".join(sections)


def build_user_message(
    *,
    repo: str,
    pr: dict,
    files: list[dict],
    diff: str,
    head_ref: str,
    commentable: dict[str, dict[str, set[int]]],
) -> str:
    closing = pr.get("closingIssuesReferences") or []
    body = pr.get("body") or "(empty PR description)"
    labels = ", ".join(
        label.get("name", "") for label in pr.get("labels") or [] if label.get("name")
    )
    author = (pr.get("author") or {}).get("login") or "unknown"

    parts = [
        "# Architectural docs (default branch — the contracts to challenge)",
        collect_arch_docs(),
        "# Pull request metadata",
        f"- URL: {pr.get('url')}",
        f"- Title: {pr.get('title')}",
        f"- Author: {author}",
        f"- Base: {pr.get('baseRefName')} <- head: {pr.get('headRefName')} (`{pr.get('headRefOid')}`)",
        f"- Labels: {labels or '(none)'}",
        "",
        "# PR description",
        body,
        "",
        "# Linked issue bodies",
        collect_issue_bodies(repo, body, closing),
        "# Complete diff",
        f"```diff\n{truncate(diff, MAX_DIFF_CHARS, 'diff')}\n```",
        "# Commentable lines",
        format_commentable_lines(commentable),
        "# Changed-file contents at PR head (numbered)",
        collect_changed_files(head_ref, files),
        "Place every BLOCKING and MAJOR finding on a commentable line.",
        "Reply with JSON only: event, body (full markdown review), comments.",
    ]
    return truncate("\n".join(parts), MAX_USER_CHARS, "user message")


def call_grok(system_prompt: str, user_message: str, model: str, api_key: str) -> str:
    payload = {
        "model": model,
        "temperature": 0.2,
        "search_parameters": {"mode": "off"},
        "prompt_cache_key": "merge-warden-v1",
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    }
    request = urllib.request.Request(
        XAI_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Grok API HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Grok API request failed: {exc}") from exc

    data = json.loads(raw)
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Grok API returned no choices: {raw[:2000]}")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not content:
        refusal = message.get("refusal") or data
        raise RuntimeError(f"Grok API returned empty content: {refusal}")
    return content.strip()


def parse_review_json(raw: str) -> dict:
    text = raw.strip()
    text = FENCE_RE.sub("", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise RuntimeError(f"Grok did not return JSON: {text[:2000]}") from None
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise RuntimeError("Grok JSON root must be an object")
    return data


SEVERITY_RANK = {
    "blocking": 3,
    "blocker": 3,
    "major": 2,
    "minor": 1,
    "suggestion": 1,
}
SEVERITY_LABEL = {
    "blocking": "BLOCKING",
    "major": "MAJOR",
    "minor": "MINOR",
}


def normalize_severity(value: str) -> str:
    raw = (value or "").strip().lower()
    if raw in {"blocking", "blocker"}:
        return "blocking"
    if raw == "major":
        return "major"
    return "minor"


def normalize_event(event: str, body: str) -> str:
    def canonical(text: str) -> str:
        key = re.sub(r"[^A-Z]+", "_", text.upper()).strip("_")
        if key in {"APPROVE", "COMMENT", "REQUEST_CHANGES"}:
            return key
        return ""

    for candidate in (event, *(line.lstrip("#").strip() for line in (body or "").splitlines()[:8])):
        mapped = canonical(candidate)
        if mapped:
            return mapped
    return "COMMENT"


def wrap_review_body(markdown: str) -> str:
    text = (markdown or "").strip()
    if not text:
        text = "# COMMENT\n\nMerge Warden returned an empty review body.\n"
    if MARKER not in text:
        text = f"{MARKER}\n{text}"
    return truncate(text, MAX_REVIEW_CHARS, "review body")


def format_inline_body(severity: str, body: str) -> str:
    text = body.strip() or "(empty comment)"
    if not text.startswith("**"):
        text = f"**{SEVERITY_LABEL[normalize_severity(severity)]}.** {text}"
    return truncate(f"{MARKER}\n{text}\n", MAX_COMMENT_CHARS, "comment")


def merge_inline_comments(comments: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str, int], dict] = {}
    order: list[tuple[str, str, int]] = []
    for comment in comments:
        key = (comment["path"], comment["side"], comment["line"])
        severity = normalize_severity(str(comment.get("severity") or "minor"))
        chunk = comment["body"].strip()
        if not chunk.startswith("**"):
            chunk = f"**{SEVERITY_LABEL[severity]}.** {chunk}"
        if key not in merged:
            merged[key] = {**comment, "severity": severity, "body": chunk}
            order.append(key)
            continue
        existing = merged[key]
        if SEVERITY_RANK[severity] > SEVERITY_RANK.get(existing.get("severity") or "minor", 0):
            existing["severity"] = severity
        existing["body"] = f"{existing['body'].rstrip()}\n\n{chunk}"
    return [merged[key] for key in order]


def build_inline_comments(
    review: dict,
    commentable: dict[str, dict[str, set[int]]],
) -> list[dict]:
    raw_comments = review.get("comments") or []
    prepared: list[dict] = []
    if isinstance(raw_comments, list):
        for item in raw_comments:
            if not isinstance(item, dict):
                continue
            snapped = snap_comment(item, commentable)
            if snapped is None:
                continue
            severity = normalize_severity(str(item.get("severity") or "minor"))
            prepared.append(
                {
                    **snapped,
                    "severity": severity,
                    "body": str(item.get("body") or "").strip(),
                }
            )

    if not prepared:
        fallback = first_commentable(commentable)
        if fallback is None:
            return []
        path, side, line = fallback
        event = normalize_event(str(review.get("event") or ""), str(review.get("body") or ""))
        prepared.append(
            {
                "path": path,
                "side": side,
                "line": line,
                "severity": "minor",
                "body": f"{event}: advertised contracts hold under the supplied diff.",
            }
        )

    prepared = merge_inline_comments(prepared)[:MAX_COMMENTS]
    return [
        {
            "path": item["path"],
            "side": item["side"],
            "line": item["line"],
            "subject_type": "line",
            "body": format_inline_body(item["severity"], item["body"]),
        }
        for item in prepared
    ]


def render_markdown(review: dict, comments: list[dict], event: str) -> str:
    body = wrap_review_body(str(review.get("body") or ""))
    extra = [
        "",
        f"_Merge Warden event `{event}`. {len(comments)} inline comment(s)._",
        "",
    ]
    return truncate(body.rstrip() + "\n" + "\n".join(extra), MAX_REVIEW_CHARS, "review")


def review_summary_body(review: dict) -> str:
    return wrap_review_body(str(review.get("body") or ""))


def delete_previous_comments(repo: str, pr_number: str) -> None:
    review_comments = gh_api_paginate_items(f"repos/{repo}/pulls/{pr_number}/comments")
    for comment in review_comments:
        body = comment.get("body") or ""
        login = ((comment.get("user") or {}).get("login") or "")
        if MARKER in body and login in BOT_LOGINS:
            run(
                ["gh", "api", "--method", "DELETE", f"repos/{repo}/pulls/comments/{comment['id']}"],
                check=False,
            )

    issue_comments = gh_api_paginate_items(f"repos/{repo}/issues/{pr_number}/comments")
    for comment in issue_comments:
        body = comment.get("body") or ""
        login = ((comment.get("user") or {}).get("login") or "")
        if MARKER in body and login in BOT_LOGINS:
            run(
                [
                    "gh",
                    "api",
                    "--method",
                    "DELETE",
                    f"repos/{repo}/issues/comments/{comment['id']}",
                ],
                check=False,
            )


def post_review(repo: str, pr_number: str, payload: dict) -> None:
    comments = list(payload.get("comments") or [])
    body = payload.get("body") or f"{MARKER}\n# COMMENT\n\nMerge Warden review.\n"
    commit_id = payload.get("commit_id")
    if not commit_id:
        raise RuntimeError("Review payload is missing commit_id")
    event = normalize_event(str(payload.get("event") or "COMMENT"), body)

    delete_previous_comments(repo, pr_number)

    events_to_try = [event]
    if event != "COMMENT":
        events_to_try.append("COMMENT")

    last_error = None
    remaining = comments
    for current_event in events_to_try:
        attempt = list(remaining)
        while True:
            review_payload = {
                "commit_id": commit_id,
                "event": current_event,
                "body": body,
            }
            if attempt:
                review_payload["comments"] = attempt
            try:
                gh_api(
                    "POST",
                    f"repos/{repo}/pulls/{pr_number}/reviews",
                    review_payload,
                )
                print(
                    f"Posted Merge Warden review event={current_event} "
                    f"with {len(attempt)} inline comment(s)"
                )
                return
            except CommandError as exc:
                last_error = exc
                if current_event != "COMMENT" and attempt == remaining:
                    print(
                        f"GitHub rejected event {current_event}; retrying as COMMENT ({exc})",
                        file=sys.stderr,
                    )
                    break
                if not attempt:
                    break
                if len(attempt) == 1:
                    if current_event == "COMMENT":
                        raise RuntimeError(
                            f"Failed to post Merge Warden review: {last_error}"
                        ) from exc
                    break
                dropped = attempt.pop()
                print(
                    f"GitHub rejected the review payload; retrying without "
                    f"{dropped.get('path')}:{dropped.get('line')} ({exc})",
                    file=sys.stderr,
                )
    raise RuntimeError(f"Failed to post Merge Warden review: {last_error}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", required=True, help="Pull request number")
    parser.add_argument(
        "--prompt-file",
        default=str(DEFAULT_PROMPT),
        help="System prompt markdown",
    )
    parser.add_argument(
        "--output",
        default="merge-warden.md",
        help="Path to write the review markdown",
    )
    parser.add_argument(
        "--json-output",
        default="merge-warden.json",
        help="Path to write the GitHub review payload",
    )
    parser.add_argument(
        "--head-ref",
        default="pr-head",
        help="Local git ref pointing at the PR head",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("XAI_MODEL", DEFAULT_MODEL),
        help="Grok model name",
    )
    parser.add_argument(
        "--post-from",
        help="Post inline comments from an existing merge-warden.json payload",
    )
    parser.add_argument(
        "--post",
        action="store_true",
        help="Post inline comments after generating the review",
    )
    return parser.parse_args()


def generate_review(args: argparse.Namespace, repo: str) -> int:
    api_key = os.environ.get("XAI_API_KEY", "")
    if not api_key:
        print(
            "::warning::XAI_API_KEY secret is not set; skipping Merge Warden.",
            file=sys.stderr,
        )
        return 0

    system_prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    pr = gh_json(
        [
            "pr",
            "view",
            args.pr,
            "--repo",
            repo,
            "--json",
            "number,title,body,url,author,baseRefName,headRefName,headRefOid,"
            "labels,closingIssuesReferences",
        ]
    )
    if not isinstance(pr, dict):
        print(f"Could not load PR #{args.pr}", file=sys.stderr)
        return 1
    files = collect_pr_files(repo, args.pr)
    commentable = commentable_by_path(files)
    diff_result = run(["gh", "pr", "diff", args.pr, "--repo", repo], check=False)
    diff = diff_result.stdout if diff_result.returncode == 0 else ""
    if diff_result.returncode != 0:
        diff = f"(failed to load complete diff: {diff_result.stderr.strip()})\n"

    user_message = build_user_message(
        repo=repo,
        pr=pr,
        files=files,
        diff=diff,
        head_ref=args.head_ref,
        commentable=commentable,
    )
    raw = call_grok(system_prompt, user_message, args.model, api_key)
    review = parse_review_json(raw)
    comments = build_inline_comments(review, commentable)
    head_sha = pr.get("headRefOid") or ""
    event = normalize_event(str(review.get("event") or ""), str(review.get("body") or ""))
    payload = {
        "commit_id": head_sha,
        "event": event,
        "body": review_summary_body(review),
        "comments": comments,
    }
    Path(args.json_output).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    Path(args.output).write_text(
        render_markdown(review, comments, event),
        encoding="utf-8",
    )
    print(
        f"Wrote {args.output} and {args.json_output} "
        f"(event={event}, {len(comments)} inline comment(s))"
    )
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as handle:
            handle.write(f"markdown-path={args.output}\n")
            handle.write(f"json-path={args.json_output}\n")
            handle.write(f"comment-count={len(comments)}\n")
            handle.write(f"event={event}\n")
    if args.post:
        post_review(repo, args.pr, payload)
    return 0


def main() -> int:
    args = parse_args()
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        print("GITHUB_REPOSITORY is not set", file=sys.stderr)
        return 1

    if args.post_from:
        payload = json.loads(Path(args.post_from).read_text(encoding="utf-8"))
        post_review(repo, args.pr, payload)
        return 0
    return generate_review(args, repo)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"::error::{exc}", file=sys.stderr)
        raise
