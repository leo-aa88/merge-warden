# Merge Warden

Composite GitHub Action that reviews a pull request with [Grok](https://docs.x.ai/)
as an adversarial senior reviewer. It posts **APPROVE**, **COMMENT**, or
**REQUEST CHANGES** with **inline comments on the diff**.

Run it **after CI passes**, and **only on pull requests**. The action does not
trigger itself — wrap it in a workflow in the consuming repo.

## Publish this as its own repository

Marketplace / `uses: OWNER/REPO@v1` requires `action.yml` at the **repository
root**. Copy these files into a new public repo:

```
action.yml
merge_warden.py
prompt.md
README.md
```

Then tag a release (`v1`, `v1.0.0`). Consumers reference that repo:

```yaml
- uses: OWNER/merge-warden@v1
```

## Usage

Repository secret `XAI_API_KEY` from https://console.x.ai/ is required.

### After another workflow succeeds (recommended)

`workflow_run` runs on the default branch, so secrets work for fork PRs and
untrusted PR code is never executed.

```yaml
name: Merge Warden

on:
  workflow_run:
    workflows: ["CI"]          # name: of the workflow that must pass first
    types: [completed]

permissions: {}

jobs:
  review:
    if: >
      github.event.workflow_run.event == 'pull_request' &&
      github.event.workflow_run.conclusion == 'success'
    runs-on: ubuntu-latest
    timeout-minutes: 20
    permissions:
      contents: read
      issues: read
      pull-requests: write
    steps:
      - uses: actions/checkout@v4

      - id: pr
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          HEAD_SHA: ${{ github.event.workflow_run.head_sha }}
        run: |
          num="$(gh api "repos/${GITHUB_REPOSITORY}/commits/${HEAD_SHA}/pulls" \
            --jq '.[0].number // empty')"
          echo "number=${num}" >> "$GITHUB_OUTPUT"

      - uses: leo-aa88/merge-warden@v1
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          xai-api-key: ${{ secrets.XAI_API_KEY }}
          pr-number: ${{ steps.pr.outputs.number }}
          arch-docs: |
            README.md
            CONTRIBUTING.md
            docs/ARCHITECTURE.md
```

### Same-repo reusable workflow

This repository also exposes `.github/workflows/merge-warden.yml`
(`workflow_call`). That wrapper resolves `uses: ./.github/actions/merge-warden`,
which only works when the **caller** is this repo. After you publish a
dedicated action repo, prefer the composite action snippet above.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `github-token` | no | `github.token` | Needs `contents: read`, `issues: read`, `pull-requests: write` |
| `xai-api-key` | yes | | xAI API key |
| `pr-number` | yes | | Pull request number |
| `prompt-file` | no | action `prompt.md` | Override the built-in Merge Warden prompt |
| `model` | no | `grok-4.6` | Grok model |
| `arch-docs` | no | common docs if present | Paths injected after the system prompt |
| `skip-names` | no | | Extra basenames to skip when attaching file contents |
| `head-ref` | no | `pr-head` | Local ref for the fetched PR head |
| `fetch-head` | no | `true` | Fetch `pull/{n}/head` |
| `post` | no | `true` | Post the GitHub review |

The action never executes PR code. It checks out the default branch (caller
must `actions/checkout`), fetches the PR head as a git ref, and reads files
with `git show`.

## Outputs

- `markdown-path` — `merge-warden.md`
- `json-path` — `merge-warden.json`
- `comment-count` — inline comments in the payload
- `event` — `APPROVE`, `COMMENT`, or `REQUEST_CHANGES`

Injected after the system prompt: architectural docs, issue bodies, PR
description, complete diff, commentable line map, and numbered changed-file
contents.
