#!/usr/bin/env python3
"""Tests for the workflow_run PR resolver (fork vs same-repo lookup)."""

from __future__ import annotations

import os
import stat
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RESOLVE_PR = ROOT / "resolve-pr" / "resolve_pr.sh"

FAKE_GH = r"""#!/usr/bin/env bash
set -euo pipefail
log="${GH_MOCK_LOG:?}"
printf '%s\n' "$*" >> "$log"

path=""
head=""
for arg in "$@"; do
  case "$arg" in
    repos/*/pulls|repos/*/commits/*/pulls)
      path="$arg"
      ;;
    head=*)
      head="${arg#head=}"
      ;;
  esac
done

if [[ "$path" == *"/commits/"*"/pulls" ]]; then
  if [ "${GH_MOCK_SHA_FAIL:-}" = "1" ]; then
    echo "commit association failed" >&2
    exit 1
  fi
  printf '%s' "${GH_MOCK_SHA_PR:-}"
  exit 0
fi

if [[ "$path" == */pulls ]]; then
  if [ "${GH_MOCK_HEAD_FAIL:-}" = "1" ]; then
    echo "head lookup failed" >&2
    exit 1
  fi
  expected="${GH_MOCK_HEAD:-}"
  if [ -n "$expected" ] && [ "$head" = "$expected" ]; then
    printf '%s' "${GH_MOCK_HEAD_PR:-}"
    exit 0
  fi
  printf '%s' ""
  exit 0
fi

echo "unexpected gh invocation: $*" >&2
exit 2
"""


class ResolvePrTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = self._tmp()
        self.gh_log = self.tmp / "gh.log"
        self.output = self.tmp / "github_output"
        self.gh = self.tmp / "gh"
        self.gh.write_text(FAKE_GH, encoding="utf-8")
        self.gh.chmod(self.gh.stat().st_mode | stat.S_IEXEC)
        self.env = {
            **os.environ,
            "PATH": f"{self.tmp}:{os.environ.get('PATH', '')}",
            "GITHUB_REPOSITORY": "Brainrotlang/brainrot",
            "GITHUB_OUTPUT": str(self.output),
            "GH_MOCK_LOG": str(self.gh_log),
            "GH_TOKEN": "test-token",
        }

    def _tmp(self) -> Path:
        import tempfile

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup, tmp)
        return tmp

    @staticmethod
    def _cleanup(tmp: Path) -> None:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    def _run(self, extra: dict[str, str], check: bool = True) -> subprocess.CompletedProcess[str]:
        env = {**self.env, **extra}
        return subprocess.run(
            ["bash", str(RESOLVE_PR)],
            env=env,
            text=True,
            capture_output=True,
            check=check,
        )

    def _gh_calls(self) -> list[str]:
        if not self.gh_log.exists():
            return []
        return self.gh_log.read_text(encoding="utf-8").splitlines()

    def test_prefers_workflow_run_pull_requests(self) -> None:
        result = self._run(
            {
                "PR_FROM_EVENT": "42",
                "HEAD_OWNER": "ChrisJr404",
                "HEAD_BRANCH": "fix/pointer-struct-member-access",
                "HEAD_SHA": "151db44087dbc32dee6016bb2f4c5c19ec090103",
                "GH_MOCK_HEAD": "ChrisJr404:fix/pointer-struct-member-access",
                "GH_MOCK_HEAD_PR": "199",
                "GH_MOCK_SHA_PR": "7",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Resolved PR #42", result.stdout)
        self.assertEqual(self.output.read_text(encoding="utf-8").strip(), "number=42")
        self.assertEqual(self._gh_calls(), [])

    def test_fork_owner_branch_when_sha_has_no_pr(self) -> None:
        # Reproduces Brainrotlang/brainrot#199 / run 32377998846:
        # fork commit is not associated with a PR on the base repo.
        result = self._run(
            {
                "HEAD_OWNER": "ChrisJr404",
                "HEAD_BRANCH": "fix/pointer-struct-member-access",
                "HEAD_SHA": "151db44087dbc32dee6016bb2f4c5c19ec090103",
                "GH_MOCK_HEAD": "ChrisJr404:fix/pointer-struct-member-access",
                "GH_MOCK_HEAD_PR": "199",
                "GH_MOCK_SHA_PR": "",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Resolved PR #199", result.stdout)
        self.assertEqual(self.output.read_text(encoding="utf-8").strip(), "number=199")
        calls = self._gh_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("head=ChrisJr404:fix/pointer-struct-member-access", calls[0])
        self.assertNotIn("/commits/", calls[0])

    def test_sha_fallback_for_same_repo_prs(self) -> None:
        result = self._run(
            {
                "HEAD_OWNER": "Brainrotlang",
                "HEAD_BRANCH": "missing-open-pr-branch",
                "HEAD_SHA": "abc123",
                "GH_MOCK_HEAD": "Brainrotlang:other",
                "GH_MOCK_HEAD_PR": "12",
                "GH_MOCK_SHA_PR": "88",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Resolved PR #88", result.stdout)
        self.assertEqual(self.output.read_text(encoding="utf-8").strip(), "number=88")
        calls = self._gh_calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("head=Brainrotlang:missing-open-pr-branch", calls[0])
        self.assertIn("commits/abc123/pulls", calls[1])

    def test_sha_fallback_when_head_lookup_errors(self) -> None:
        result = self._run(
            {
                "HEAD_OWNER": "ChrisJr404",
                "HEAD_BRANCH": "fix/pointer-struct-member-access",
                "HEAD_SHA": "151db44087dbc32dee6016bb2f4c5c19ec090103",
                "GH_MOCK_HEAD_FAIL": "1",
                "GH_MOCK_SHA_PR": "199",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Resolved PR #199", result.stdout)

    def test_fails_when_nothing_resolves(self) -> None:
        result = self._run(
            {
                "HEAD_OWNER": "ChrisJr404",
                "HEAD_BRANCH": "fix/pointer-struct-member-access",
                "HEAD_SHA": "151db44087dbc32dee6016bb2f4c5c19ec090103",
                "GH_MOCK_HEAD": "ChrisJr404:fix/pointer-struct-member-access",
                "GH_MOCK_HEAD_PR": "",
                "GH_MOCK_SHA_PR": "",
            },
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "Could not resolve PR for ChrisJr404:fix/pointer-struct-member-access "
            "(151db44087dbc32dee6016bb2f4c5c19ec090103)",
            result.stdout + result.stderr,
        )
        self.assertFalse(self.output.exists())

    def test_ignores_non_numeric_event_pr(self) -> None:
        result = self._run(
            {
                "PR_FROM_EVENT": "null",
                "HEAD_OWNER": "ChrisJr404",
                "HEAD_BRANCH": "fix/pointer-struct-member-access",
                "HEAD_SHA": "deadbeef",
                "GH_MOCK_HEAD": "ChrisJr404:fix/pointer-struct-member-access",
                "GH_MOCK_HEAD_PR": "199",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Resolved PR #199", result.stdout)


if __name__ == "__main__":
    unittest.main()
