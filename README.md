# Merge Warden

Composite GitHub Action that reviews a pull request with an adversarial senior
reviewer. It posts **APPROVE**, **COMMENT**, or **REQUEST CHANGES** with
**inline comments on the diff**.

Supported providers:

| Provider | `provider` value | Secret / input | Default model |
| --- | --- | --- | --- |
| [Grok](https://docs.x.ai/) (default) | `xai` or `grok` | `xai-api-key` / `XAI_API_KEY` | `grok-4.6` |
| [ChatGPT](https://platform.openai.com/docs/) | `openai` or `chatgpt` | `openai-api-key` / `OPENAI_API_KEY` | `gpt-4.1` |
| [Claude](https://docs.anthropic.com/) | `anthropic` or `claude` | `anthropic-api-key` / `ANTHROPIC_API_KEY` | `claude-sonnet-4-6` |
| [Gemini](https://ai.google.dev/gemini-api/docs) | `google` or `gemini` | `google-api-key` / `GOOGLE_API_KEY` | `gemini-2.5-pro` |

Pass `model` to override the default. Gemini also accepts `GEMINI_API_KEY`.

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

Set the API key secret for the provider you choose. Existing Grok workflows
keep working: `provider` defaults to `xai`.

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

### ChatGPT, Claude, or Gemini

Swap the provider and the matching API key. Leave `model` unset to use the
provider default, or set it to a specific model id.

```yaml
- uses: leo-aa88/merge-warden@v1
  with:
    github-token: ${{ secrets.GITHUB_TOKEN }}
    provider: openai          # chatgpt, anthropic, claude, google, gemini
    openai-api-key: ${{ secrets.OPENAI_API_KEY }}
    # anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    # google-api-key: ${{ secrets.GEMINI_API_KEY }}
    # model: gpt-4o
    pr-number: ${{ steps.pr.outputs.number }}
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
| `provider` | no | `xai` | `xai`/`grok`, `openai`/`chatgpt`, `anthropic`/`claude`, or `google`/`gemini` |
| `xai-api-key` | for `xai` | | xAI API key |
| `openai-api-key` | for `openai` | | OpenAI API key |
| `anthropic-api-key` | for `anthropic` | | Anthropic API key |
| `google-api-key` | for `google` | | Gemini API key (`GEMINI_API_KEY` also works) |
| `pr-number` | yes | | Pull request number |
| `prompt-file` | no | action `prompt.md` | Override the built-in Merge Warden prompt |
| `model` | no | provider default | Model id for the selected provider |
| `arch-docs` | no | common docs if present | Paths injected after the system prompt |
| `skip-names` | no | | Extra basenames to skip when attaching file contents |
| `head-ref` | no | `pr-head` | Local ref for the fetched PR head |
| `fetch-head` | no | `true` | Fetch `pull/{n}/head` |
| `post` | no | `true` | Post the GitHub review |

If the selected provider's API key is unset, the action skips the review
instead of failing the job.

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
