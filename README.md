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

```yaml
- uses: leo-aa88/merge-warden@PINNED_SHA
```

Pin the commit. The moving `v1` tag may lag `main`.

## Used by

- [Brainrotlang/brainrot](https://github.com/Brainrotlang/brainrot) — the Brainrot programming language

## Usage

Set the API key secret for the provider you choose. Existing Grok workflows
keep working: `provider` defaults to `xai`.

A missing provider API key **fails the job**. Set `skip-if-missing-key: true`
only if the review should be optional.

### After another workflow succeeds (recommended)

`workflow_run` runs on the default branch, so secrets work for fork PRs and
untrusted PR code is never executed.

Do not resolve the PR from `head_sha` alone. GitHub's commit-association API
searches the *base* repository, so a fork commit often has no linked PR even
when the triggering run was a `pull_request` event. `workflow_run.pull_requests`
is also frequently empty. Use `leo-aa88/merge-warden/resolve-pr`, which tries
`workflow_run.pull_requests[0]`, then `HEAD_OWNER:HEAD_BRANCH`, then the head
SHA. Pin **both** `resolve-pr` and `merge-warden` to the same commit SHA;
moving `v1` does not update SHA-pinned workflows.

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
    timeout-minutes: 30
    permissions:
      contents: read
      issues: read
      pull-requests: write
    steps:
      - uses: actions/checkout@v4

      - name: Resolve pull request
        id: pr
        uses: leo-aa88/merge-warden/resolve-pr@PINNED_SHA
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}

      - name: Review PR
        uses: leo-aa88/merge-warden@PINNED_SHA
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          xai-api-key: ${{ secrets.XAI_API_KEY }}
          pr-number: ${{ steps.pr.outputs.number }}
          expected-head-sha: ${{ github.event.workflow_run.head_sha }}
          arch-docs: |
            README.md
            CONTRIBUTING.md
            docs/ARCHITECTURE.md
```

Replace `PINNED_SHA` with the same commit in both steps. `expected-head-sha`
makes Merge Warden skip if the PR moved after the CI run that triggered
`workflow_run`. The next successful CI run reviews the new head.

### ChatGPT, Claude, or Gemini

Swap the provider and the matching API key. Leave `model` unset to use the
provider default, or set it to a specific model id.

```yaml
- uses: leo-aa88/merge-warden@PINNED_SHA
  with:
    github-token: ${{ secrets.GITHUB_TOKEN }}
    provider: openai          # chatgpt, anthropic, claude, google, gemini
    openai-api-key: ${{ secrets.OPENAI_API_KEY }}
    # anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    # google-api-key: ${{ secrets.GEMINI_API_KEY }}
    # model: gpt-4o
    pr-number: ${{ steps.pr.outputs.number }}
```

## GitHub review events

Merge Warden may *generate* `APPROVE`, `COMMENT`, or `REQUEST_CHANGES`.
`GITHUB_TOKEN` cannot approve pull requests unless the repository enables
[Allow GitHub Actions to create and approve pull requests](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository).

If GitHub rejects `APPROVE` or `REQUEST_CHANGES`, the action posts a `COMMENT`
instead of failing. Use `generated-event` / `posted-event` (and the matching
comment-count outputs) rather than assuming the generated event was published.

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
| `skip-if-missing-key` | no | `false` | Skip instead of failing when the provider key is unset |
| `expected-head-sha` | no | | Skip if the PR head is no longer this SHA (`workflow_run.head_sha`) |
| `review-timeout-seconds` | no | `900` | Internal wall-clock review budget; exhaustion fail-closes to `COMMENT` |
| `shutdown-reserve-seconds` | no | `60` | Time reserved inside the review budget for outputs and posting |
| `map-concurrency` | no | `4` | Max independent map provider requests in flight (1–8) |
| `validation-concurrency` | no | `2` | Max independent validation provider requests in flight (1–4) |

The action never executes PR code. It checks out the default branch (caller
must `actions/checkout`), fetches the PR head as a git ref, and reads files
with `git show`.

## Outputs

- `markdown-path` — `merge-warden.md`
- `json-path` — `merge-warden.json`
- `generated-event` — event the model produced
- `generated-comment-count` — inline comments before GitHub accepts the review
- `posted-event` — event GitHub accepted (empty when `post` is false)
- `posted-comment-count` — inline comments GitHub accepted (empty when `post` is false)
- `event` — `posted-event` when posting, otherwise `generated-event`
- `comment-count` — `posted-comment-count` when posting, otherwise `generated-comment-count`

Injected after the system prompt for the primary pass: architectural docs,
issue bodies, PR description, and the complete diff. Changed-file source is
not eagerly mapped as full files; when the map stage emits `needs_context`,
Merge Warden loads the requested file from the PR head and sends numbered
source chunks through targeted validation. That material is treated as
untrusted data, not as instructions to the reviewer.

Merge Warden does **not** truncate the PR to fit a context window. It builds
a complete primary context corpus, splits it at semantic boundaries (diff
hunks and headings), packs those chunks into bounded map calls (~225k
characters **and** at most 8 chunks each), extracts structured evidence,
pre-reduces equivalent mapper findings onto canonical survivors, runs a
targeted source validation pass against those survivors when they still
request more context, then hierarchically reduces finding IDs again
(without rewriting their original bodies) and synthesizes the GitHub
review. Independent map batches run concurrently (default 4 in-flight
provider calls) so provider latency overlaps. After pre-reduce, remaining
cross-context validation is ordered by finding severity (BLOCKING, then
MAJOR, then MINOR) and merge-decision impact, then independent paths run
with a separate, more conservative worker pool (default 2 in-flight,
max 4). Evidence ingestion stays single-threaded and deterministic.
Failed map batches split into smaller requests instead of abandoning sibling
chunks; a global cap of 32 logical map attempts still fail-closes the review.

Binary and generated files can be excluded, but that exclusion is explicit in
the PR index. If reviewable context exceeds `MERGE_WARDEN_MAX_TOTAL_REVIEW_CHARS`
(default 10 MB) or `MERGE_WARDEN_MAX_CONTEXT_CHUNKS` (default 64 after
coalescing), or if any reviewable chunk is not analyzed, Merge Warden posts
`COMMENT` and **does not approve**. It will not silently drop the tail of a
diff and then emit `# APPROVE`.

Each map/reduce call is capped around 225k characters so provider connections
stay reliable. Map calls are also capped at 8 chunks so the required
structured response stays bounded independently of input size. Independent
map batches share a bounded worker pool (`map-concurrency`, default 4, max
8). Independent validation paths share a separate pool
(`validation-concurrency`, default 2, max 4) so a large validation request
cannot inherit map's higher fan-out. Workers only perform provider I/O;
coverage, evidence, retry/split, and lazy context loading stay on one
thread. The pipeline footer reports primary map, raw vs pre-reduced finding
counts, validation attempts, validation concurrency, deferred validation
paths, reduce, synthesis, and total request characters so corpus changes can
be benchmarked. The total source size is not artificially bounded by
those per-call budgets.

Merge Warden also enforces an internal wall-clock review budget. The default is
900 seconds, with the final 60 seconds reserved for writing artifacts and
posting the GitHub review. Inside the remaining provider budget, the last 120
seconds are reserved for reduction and the last 150 seconds for final
synthesis. Provider request timeouts and retry sleeps are clamped to the
stage cutoff:

- pre-reduce and validation stop `270s` before the provider cutoff
- reduce stops `150s` before the provider cutoff
- synthesis uses the remaining provider budget

Once a stage cutoff is reached, that stage stops scheduling new provider
calls and the pipeline continues. Remaining cross-context checks are marked
`validation:incomplete:<path>`. Remaining reduce groups are kept. Incomplete
validation is acceptable; a review with no synthesis is not. Map still uses
the full provider deadline; exhausting it fail-closes to `COMMENT` because
primary coverage is incomplete. If synthesis itself hits the provider cutoff,
the pipeline preserves the evidence collected so far and returns a fail-closed
`COMMENT` instead of waiting for the outer GitHub Actions timeout to kill the
process. Keep the workflow job timeout comfortably above
`review-timeout-seconds`.

Optional environment overrides:

| Variable | Default | Meaning |
| --- | --- | --- |
| `MERGE_WARDEN_MAX_MAP_REQUEST_CHARS` | 225000 | Per map-call budget |
| `MERGE_WARDEN_MAX_REDUCE_REQUEST_CHARS` | 225000 | Per reduce/synthesis-call budget |
| `MERGE_WARDEN_MAX_SINGLE_CHUNK_CHARS` | 100000 | Max size of one context chunk |
| `MERGE_WARDEN_MAX_TOTAL_REVIEW_CHARS` | 10000000 | Hard cap on reviewable source text |
| `MERGE_WARDEN_MAX_CONTEXT_CHUNKS` | 64 | Hard cap on coalesced reviewable chunks |
| `MERGE_WARDEN_MAX_LAZY_CONTEXT_BYTES` | 1000000 | Max blob size for one lazily loaded validation file |
| `MERGE_WARDEN_REVIEW_TIMEOUT_SECONDS` | 900 | Total internal wall-clock review budget |
| `MERGE_WARDEN_SHUTDOWN_RESERVE_SECONDS` | 60 | Time reserved for outputs and posting |
| `MERGE_WARDEN_MAP_CONCURRENCY` | 4 | Max independent map provider requests in flight (1–8) |
| `MERGE_WARDEN_VALIDATION_CONCURRENCY` | 2 | Max independent validation provider requests in flight (1–4) |

GitHub review bodies and inline comments still have posting size limits
(60k / 8k). Those are GitHub API limits, not source truncation.

Provider HTTP calls retry transient failures (disconnects, timeouts,
HTTP 429/5xx) a few times with exponential backoff. HTTP 429 honors
`Retry-After` when present, capped at 60 seconds. Every provider socket timeout
and retry delay is also bounded by the remaining internal review budget.
