#!/usr/bin/env bash
# Resolve a workflow_run PR number for same-repo and fork pull requests.
#
# Lookup order:
#   1. workflow_run.pull_requests[0].number (PR_FROM_EVENT)
#   2. HEAD_OWNER:HEAD_BRANCH (reliable for fork PRs)
#   3. commits/HEAD_SHA/pulls (same-repo / unusual cases)
set -euo pipefail

pr_number=""
if [[ "${PR_FROM_EVENT:-}" =~ ^[0-9]+$ ]]; then
  pr_number="${PR_FROM_EVENT}"
fi

# Fork PRs: look up by head owner + branch. The commit-association API
# searches the base repo, so fork SHAs often have no linked PR.
if [ -z "$pr_number" ] && [ -n "${HEAD_OWNER:-}" ] && [ -n "${HEAD_BRANCH:-}" ]; then
  pr_number="$(
    gh api --method GET \
      "repos/${GITHUB_REPOSITORY}/pulls" \
      -f state=open \
      -f head="${HEAD_OWNER}:${HEAD_BRANCH}" \
      --jq '.[0].number // empty' \
    || true
  )"
fi

if [ -z "$pr_number" ] && [ -n "${HEAD_SHA:-}" ]; then
  pr_number="$(
    gh api \
      "repos/${GITHUB_REPOSITORY}/commits/${HEAD_SHA}/pulls" \
      --jq '.[0].number // empty' \
    || true
  )"
fi

if [ -z "$pr_number" ]; then
  echo "::error::Could not resolve PR for ${HEAD_OWNER:-unknown}:${HEAD_BRANCH:-unknown} (${HEAD_SHA:-unknown})"
  exit 1
fi

echo "Resolved PR #${pr_number}"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "number=${pr_number}" >> "$GITHUB_OUTPUT"
fi
