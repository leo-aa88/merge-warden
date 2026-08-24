---
name: feature
description: Use when adding or changing Merge Warden behavior, such as a new action input or output, a pipeline stage, provider support, a prompt change, or an environment override. Enforces contract-first design, the repository's fail-closed invariants, adversarial tests, and the docs that must move with the code.
---

# Feature work

Merge Warden decides whether other people's code gets merged. A feature that
is half-wired does not degrade gracefully here, it produces a confident review
of a PR it never fully read. Build the whole path or do not ship the feature.

Read `AGENTS.md` first for the repository map and the project invariants.

## 1. State the contract before writing code

Write down, in two or three sentences:

- the observable behavior a caller can rely on
- the invariant that must hold while it runs
- what happens when it fails

If you cannot state these, the design is not ready and no amount of code will
fix that. Put the same statement in the PR description later so a reviewer can
check the implementation against it rather than against your intent.

## 2. Find every surface the contract touches

A feature is not done when the code works. It is done when nothing in the
repository still describes the old behavior. For a new action input, that means
all five of these move together:

- `action.yml` — the input, its description, and a default that preserves
  existing behavior
- the composite step's `env:` block and the `python3` invocation, if the value
  reaches the CLI
- `merge_warden.py` — the CLI argument or environment read, plus its default
  constant
- `README.md` — the Inputs table, the Outputs list, or the environment override
  table
- a test that pins the default and a test that pins the overridden value

Consumers pin this action by commit SHA. Adding an input with a default is
backward compatible. Renaming or removing an input or output, or changing a
default, breaks callers silently on their next SHA bump. Say so explicitly in
the PR description when you do it.

## 3. Design against the project invariants

These are not style preferences. Violating one is a blocking defect.

- **Standard library only.** Python 3.12, no third-party packages, no
  `requirements.txt`, no build step. If you are reaching for a dependency, you
  are solving the wrong problem.
- **Fail closed, never fail open.** Any new stage that can fail, time out, or
  come back incomplete must degrade to `COMMENT`. Nothing you add may make
  `APPROVE` reachable while coverage is incomplete.
- **No silent truncation.** Anything you add to the review corpus must be
  chunked, packed, and accounted for in the coverage report. Dropping the tail
  of a diff and then emitting a verdict is the one failure this tool exists to
  prevent.
- **Bound every provider call.** New provider work needs a request-character
  budget, a retry or split path for failures, and it must respect the remaining
  wall-clock review budget rather than the outer job timeout.
- **Keep concurrency at the I/O edge.** Worker threads perform provider I/O
  only. Evidence ingestion, coverage accounting, and retry/split decisions stay
  single-threaded and deterministic.
- **Untrusted input stays untrusted.** Repository and PR content is evidence,
  never instructions. If you add a new context source, it enters through the
  same boundary as the others.
- **Reduce stages decide IDs, not prose.** They keep, reject, or merge finding
  IDs. They do not rewrite finding bodies, and merging must not drop a
  `BLOCKING` severity, a `validation:incomplete:` marker, or a requested
  context path.

## 4. Prompt changes are interface changes

`prompt.md`, `prompt_map.md`, and `prompt_reduce.md` are shipped artifacts, and
the map and reduce prompts specify JSON schemas that the Python code parses. If
you change a schema in a prompt, change the parser and its tests in the same
commit. A prompt that requests a field nobody reads is not a feature.

## 5. Test adversarially

The bar is not "there is a test." The bar is the question the review skill will
ask you:

> What incorrect implementation would still pass these tests?

For anything non-trivial, cover the boundary and the failure, not just the
happy path: zero and empty inputs, the exact limit and one past it, a provider
failure mid-batch, deadline exhaustion, failure after partial success, and the
interaction with the feature nearest yours.

Tests are hermetic. Mock provider calls and `gh`. No network, no sleeping for
real time. The suite runs in about a second and must stay that way.

```bash
python3 -m unittest          # whole suite
python3 -m unittest -v       # what CI runs
```

## 6. Review your own diff before opening the PR

Run the `review` skill against your own change and fix what it finds. Shipping
a feature to this repository without surviving its own reviewer is
embarrassing.

## Definition of done

- [ ] The contract is written down and the implementation matches it
- [ ] Every surface from step 2 is updated, no stale description remains
- [ ] Standard library only, and the fail-closed invariants still hold
- [ ] Failure and boundary tests exist, not only happy-path tests
- [ ] `python3 -m unittest` passes
- [ ] `README.md` matches the new behavior
- [ ] Breaking changes to `action.yml` are called out in the PR description
- [ ] The `review` skill has been run on the diff
