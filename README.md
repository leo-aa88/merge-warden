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
| [Gemini](https://ai.google.dev/gemini-api/docs) | `google` or `gemini` | `google-api-key` / `GOOGLE_API_KEY` | `gemini-3.1-pro-preview` |

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
Pass a PAT or GitHub App token as `github-token` when you need `APPROVE` to
post. Previous Merge Warden comments (inline and conversation) are replaced
only when they contain the HTML marker `<!-- merge-warden -->` **and** were
authored by the same GitHub identity that is posting the new review. The
marker is necessary, not sufficient: a pasted `<!-- merge-warden -->` on a
human or foreign-bot comment is not owned and is not deleted. Posting identity
comes from the credentials in use (`GET /user` login for a PAT, `{app_slug}[bot]`
from `GET /installation` for a GitHub App). `GITHUB_ACTIONS` identifies the
execution environment, not the credential identity, and `GITHUB_ACTOR` is the
triggering user; neither is used as proof of ownership. If identity lookup
fails, the new review is still posted and previous comments are left in
place — duplicates are preferable to deleting another author's threads.
Replacement DELETE runs only after GitHub accepts the new review; a failed
post also leaves previous threads in place.

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
- `json-path` — `merge-warden.json`, the GitHub review payload
  (`commit_id`, `event`, `body`, `comments`)
- `generated-event` — event the model produced
- `generated-comment-count` — inline comments before GitHub accepts the review
- `posted-event` — event GitHub accepted (empty when `post` is false)
- `posted-comment-count` — inline comments GitHub accepted (empty when `post` is false)
- `event` — `posted-event` when posting, otherwise `generated-event`
- `comment-count` — `posted-comment-count` when posting, otherwise `generated-comment-count`

Injected after the system prompt for the primary pass: architectural docs,
issue bodies, PR description, and the complete diff. Linked issues come from
GitHub's `closingIssuesReferences` first, then a body scrape capped at 20.
The scrape collects `Fixes #N` (and the other close/fix/resolve forms) and
token `#N` after whitespace, `(`, or `[`. It does not treat `C#12`,
`Python#3`, `step #1`, or `PR #238` as issues. Paired fenced code blocks are
skipped so an example `#1` is not fetched. If `gh pr diff` fails,
Merge Warden reconstructs hunks from the per-file `patch` fields already
returned by the Pulls Files API and treats those hunks as review evidence.
`APPROVE` stays unreachable unless every non-excluded changed file with
line changes is represented in that reconstruction (`limit_error` /
incomplete coverage). Skipped binaries and files with 0 additions and 0
deletions do not require a patch. The `gh pr diff` error string is never
treated as reviewed source. Inline comments are anchored to path/side/line
triples parsed from that same complete `gh pr diff` when it loads. Numeric
lines still snap to the nearest commentable line in the same file. Missing
or non-numeric `line` values (`N/A`, `null`, omitted, bools, floats) are
dropped rather than snapped to line 1; the finding stays in the review body
only if synthesis already put it there. GitHub's Pulls Files API omits
`patch` above ~20 KB / 3000 lines; commentable lines for those files still
come from the unified diff rather than an empty map.
If `gh pr diff` fails, commentable lines fall back to per-file `patch`
fields, so a large file with an omitted patch stays un-commentable rather
than inventing anchors. Changed-file source is not eagerly mapped as
full files; when the map stage emits `needs_context`, Merge Warden loads
the requested file from the PR head and sends numbered source chunks
through targeted validation. That material is treated as untrusted data,
not as instructions to the reviewer.

Merge Warden does **not** truncate the PR to fit a context window. It builds
a complete primary context corpus, splits it at semantic boundaries (diff
hunks and headings), packs those chunks into bounded map calls (~225k
characters **and** at most 8 chunks each), extracts structured evidence,
pre-reduces equivalent mapper findings onto canonical survivors, runs a
targeted source validation pass against those survivors when they still
request more context, then hierarchically reduces finding IDs again
(without rewriting their original bodies) and synthesizes the GitHub
review. Diff chunk headers in the map prompt use `lines=` for the new-file
range those hunks cover (first `+` or context line through the last), not
the number of unified-diff lines in the chunk. LEFT-only deletion hunks
fall back to the old-file range so the mapper is not given a raw 1-N count.
Oversized hunk splits continue that file-line cursor across pieces instead
of adding newline counts onto a hunk start. Independent map batches run
concurrently (default 4 in-flight provider calls) so provider latency
overlaps. After pre-reduce, remaining
cross-context validation is ordered by finding severity (BLOCKING, then
MAJOR, then MINOR) and merge-decision impact. Mapper aliases such as
`BLOCKER` canonicalize to `BLOCKING` before ranking and merge joins;
unknown labels collapse to MINOR so they cannot rank below it. Independent
paths then run with a separate, more conservative worker pool (default 2
in-flight, max 4). Evidence ingestion stays single-threaded and deterministic.
Failed map batches split into smaller requests instead of abandoning sibling
chunks; a global cap of 32 logical map attempts still fail-closes the review.
Initial map batches are balanced by chunk size (largest-first bin packing with
a soft ~16k-character target) so one giant request is not scheduled beside
several tiny ones. Hard per-request character limits still win.

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
posting the GitHub review. Inside the remaining provider budget, earlier
stages may surrender unused time to later stages, but they may never consume
time reserved for later stages:

- map stops `420s` before the provider cutoff (`150s` validation + `120s`
  reduce + `150s` synthesis)
- pre-reduce and validation stop `270s` before the provider cutoff
- reduce stops `150s` before the provider cutoff
- synthesis uses the remaining provider budget

Map calls also have a tighter per-call latency budget (140s HTTP timeout, 150s
logical call budget, 1 HTTP attempt) so valid Grok responses longer than 90s can
complete while slow batches still split before downstream reserves are
threatened. Map performs no in-call retry: a single attempt returns its failure
to the map scheduler, which can defer a batch without holding a worker thread
and keeps the failure classification intact.

The 150s figure is a ceiling, not a requirement that every dispatch reserve the
worst case. After two successful map calls, retry planning may size a standard
follow-up from the slowest success times a 1.2 safety factor, still capped at
150s and floored at 45s. Independently, while primary chunks remain uncovered
and the remaining map window is below a full call budget but at least 45s, map
spends that tail on a **coverage-repair** attempt whose HTTP timeout is sized
to the remaining window (remaining minus the 10s call-budget margin). A
shortened attempt is still an attempt: if it times out or errors, the chunk
stays uncovered and `APPROVE` stays unreachable. Remaining time below 45s is
abandoned honestly rather than burned on a doomed few-second call. Repair never
consumes the reduce or synthesis reserves, and it is suppressed while the
provider circuit is open or after a permanent provider error. Unused map time
that repair does not use is still surrendered to later stages; the reverse
remains forbidden.

How a failed map batch recovers depends on why it failed:

| Failure | Recovery |
| --- | --- |
| Permanent provider/config (HTTP 401/403, retired-model 404, invalid-model 400) | Stop provider work immediately. Never retry or split: changing request shape cannot fix a bad credential or retired model |
| Request too large (HTTP 413, context-length 400) | Split a multi-chunk request (same recovery as a multi-chunk latency timeout); request shape can change the outcome |
| Capacity (HTTP 429/503) | Re-send the same request after a backoff, up to four times. Never split: a provider shedding load is not asking for a smaller request, and splitting would double the request rate against it |
| Latency timeout, multi-chunk | Split immediately rather than repeat an expensive shape |
| Latency timeout, single chunk | Re-send under a widened clock (1.5x the failed attempt, capped at 240s), since the same clock would likely time out again |
| Transport fault | Re-send the same request |
| Malformed or partial response | Split, or re-send a single chunk |

Isolated capacity failures still retry. Several independent 429/503 responses from the same provider and model in one review run mean the endpoint is broadly unavailable, not that one batch was unlucky. After three capacity failures from at least two distinct logical requests, or a `Retry-After` that exceeds the remaining stage budget, Merge Warden **opens a provider circuit**: it stops scheduling new provider calls (retries, splits, remaining map batches, remaining validation paths, and later stages including synthesis), preserves evidence and coverage already collected, and fail-closes to `COMMENT`. Concurrent stages record those failures when the worker future lands, before sequential ingest, so a free slot cannot start more work after a 503 already exists. In-flight requests may finish and their successes are still ingested; a validation path that succeeds after the breaker opens is not marked `validation:incomplete`. A successful response resets the failure streak, so `503 → 503 → success` does not open the circuit. Latency timeouts do not count as availability failures; the split/widen policy above is unchanged.

Circuit-open is not stage-budget exhaustion. The map-stage cutoff still leaves downstream reserves intact and continues to synthesis when the provider is healthy. A provider circuit means remaining time exists but the configured provider/model is unhealthy, so Merge Warden does not call it again merely to synthesize a failure message. The pipeline footer reports `provider circuit open` separately from `map budget exhausted` and `deadline exhausted`.

Capacity backoff honors a provider `Retry-After` header when present, capped at
60s, and otherwise backs off 4s, 8s, 16s, 30s. A deferred batch does not hold a
worker while it waits, and the scheduler only waits once no completed or
in-flight work remains to process. The map stage spends at most 120s in total
waiting out capacity backoff, so one flapping batch cannot sleep away the
allocation that other batches still need. Repeated 429s across independent
requests still feed the circuit; a `Retry-After` larger than the remaining
stage budget opens it rather than sleeping the review to death.

Every retry is bounded by the **remaining map budget**, not by a fixed counter.
Merge Warden schedules another attempt only when the stage can still fund the
same dispatch envelope the call will consume: the standard 150s call budget, or
a bounded estimate of observed successful latency once enough samples exist, or
the widened HTTP timeout plus its 10s margin, plus any capacity backoff still
owed. The elapsed time of the attempt that just failed is sunk cost and is not
part of that question — a 24s 503 does not retry because 24s remains; it retries
because a full call envelope plus backoff still fits. Fixed ceilings remain as
a second, independent bound against a provider that rejects instantly; either
bound is enough to give up. They are counted per chunk, so splitting a batch
does not hand its halves a fresh retry allowance. A map call is not started
unless that same envelope still fits, which for a widened retry is larger than
the standard one; a widened retry that no longer fits leaves its chunk uncovered
rather than re-running under the clock that already failed. Untried chunks in
the tail of the map window are the exception: they may start as a coverage-repair
attempt under a shortened clock, as described above, instead of being abandoned
while tens of seconds remain.

When a capacity retry, a widened latency retry, or a coverage-repair attempt
happens, the pipeline footer in `merge-warden.md` reports it, so these paths
are visible in the job log.

Exhausting the map-stage allocation leaves remaining primary chunks uncovered
and **continues** into validation, reduction, and synthesis. Exhausting the
global provider deadline still fail-closes immediately. Uncovered chunks always
make `APPROVE` unreachable, however they came to be uncovered. A provider
circuit that leaves coverage or validation incomplete likewise makes `APPROVE`
unreachable.

Once a stage cutoff is reached, that stage stops scheduling new provider
calls and the pipeline continues. Remaining cross-context checks are marked
`validation:incomplete:<path>`, where `<path>` is the `_context_path_key`
identity (backticks and leading `./` stripped). Aliases of the same file
share one validation slot and one marker. Remaining reduce groups are kept.
Incomplete validation is acceptable; a review with no synthesis is not. A
recorded incomplete validation (including after the dependent finding is
rejected) makes `APPROVE` unreachable, using the same event normalizer as
posting, even when primary coverage is complete. Empty `incomplete_context`
and no `validation:incomplete:` markers still allow `APPROVE`. If
synthesis itself hits the provider cutoff, or returns a body that is not a
JSON object, the pipeline fail-closes to
`COMMENT` with **no inline comments**. If map-stage exhaustion leaves primary
coverage incomplete, synthesis still runs when time remains and must not
`APPROVE`;
synthesized comments from covered chunks may be posted. Mapper candidates stay
in `merge-warden.md` for debugging when synthesis does not complete;
`merge-warden.json` remains the GitHub review payload (`commit_id`, `event`,
`body`, `comments`) and is not a debug dump. An unsynthesized mapper candidate
is not a review finding. Keep the workflow job timeout comfortably above
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
| `MERGE_WARDEN_MAP_MIN_REPAIR_SECONDS` | 45 | Floor below which an uncovered chunk is abandoned rather than given a shortened map call |
| `MERGE_WARDEN_MAP_LATENCY_SAFETY_FACTOR` | 1.2 | Multiplier on the slowest successful map call when estimating the next dispatch envelope |
| `MERGE_WARDEN_MAP_LATENCY_ESTIMATE_SAMPLES` | 2 | Successful map calls required before the dispatch gate may use the latency estimate |

GitHub review bodies and inline comments still have posting size limits
(60k / 8k). Those are GitHub API limits, not source truncation. A review is
also limited to 25 inline comments. After merging comments that share a
path/side/line, Merge Warden keeps BLOCKING, then MAJOR, then MINOR (stable
within a rank) and appends any overflow to the posted review body so a
blocker is never dropped to keep 25 MINOR notes.

Provider HTTP calls outside the map stage retry transient failures
(disconnects, timeouts, HTTP 429/5xx) a few times with exponential backoff.
Map provider calls use one HTTP attempt and hand the classified failure to the
map scheduler, which owns all map backoff. Permanent provider and configuration
failures (HTTP 401/403, unknown/retired-model or wrong-endpoint 404, and HTTP
400 bodies that report an invalid model or configuration) stop new provider
work immediately across the pipeline and fail closed to `COMMENT`; they are
never retried or split. HTTP 413 and context-length / payload-too-large 400
responses are not permanent: the scheduler splits a multi-chunk request the
same way it does for a multi-chunk latency timeout. The scheduler re-sends the
same request for a capacity rejection, re-sends a single chunk under a longer
timeout after a latency timeout, and splits a multi-chunk request only when
request shape could plausibly be the problem. HTTP 429 honors `Retry-After`
when present, capped at 60 seconds. Every provider socket timeout and retry
delay is also bounded by the remaining internal review budget.
