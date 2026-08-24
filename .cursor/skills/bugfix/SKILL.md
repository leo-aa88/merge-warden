---
name: bugfix
description: Use when fixing a defect, regression, crash, or wrong review output in Merge Warden. Enforces reproduce-first with a failing test, root-cause over symptom, and a regression test that would have caught the bug.
---

# Bug fixing

Read `AGENTS.md` first for the repository map and the project invariants.

## 1. Reproduce it before you fix it

Write the failing test first. Run it. Watch it fail for the reason you expect,
not for some unrelated reason.

A change without a test that failed before it and passes after it is not a
fix, it is a guess that happens to compile. If you genuinely cannot reproduce
the bug in a test, say so in the PR description and explain what evidence you
do have. Do not quietly skip this step.

```bash
python3 -m unittest test_merge_warden      # one module
python3 -m unittest -v                     # whole suite, as CI runs it
```

## 2. Find the root cause, not the symptom

Ask which invariant was violated, then ask why the code allowed that state to
exist at all. Fix the invariant when you can.

If you fix the symptom instead, you are adding a special case, and the next
person inherits both the special case and the original defect. When a special
case really is the right call, write down in the PR why the invariant could not
be fixed.

Then ask the question that separates a fix from a patch:

> Does this same shape exist anywhere else?

The bug you found is a data point about the design. Look for sibling call sites
with the same missing guard and fix them together, or state clearly that you
checked and they are safe.

## 3. Classify the blast radius

Some bugs here are worse than they look. Escalate your thoroughness when the
defect can:

- **Fail open** — make `APPROVE` reachable while coverage is incomplete, a
  deadline was hit, or a chunk went unanalyzed. This is the worst class of bug
  in this repository. It produces a silent false approval, which is exactly
  what the tool exists to prevent.
- **Lose evidence** — drop findings, drop severity, or drop coverage
  accounting.
- **Leak the trust boundary** — let repository or PR content act as
  instructions instead of evidence.
- **Break the action contract** — change observable `action.yml` input or
  output behavior for SHA-pinned consumers.

Say which class the bug falls into in the PR description.

## 4. Known trap areas

Past regressions in this codebase clustered here. If your fix touches one of
these, test that specific interaction:

- **Coverage on failure paths.** A failed or split map batch must degrade into
  smaller requests without dropping sibling chunks or losing coverage.
- **Deadline exhaustion.** Every stage must preserve the evidence collected so
  far and return a fail-closed `COMMENT`, not lose the run.
- **Reducer merges.** Merging findings must join severity, confidence,
  evidence, and requested context. Choosing a canonical finding must not
  demote a `BLOCKING`, drop a `validation:incomplete:` marker, or discard a
  needed context path.
- **Finding IDs are chunk-local.** Independent chunks legitimately both emit
  `F1`. Anything treating them as globally unique is broken.
- **Lazy context path lookup.** Paths are used verbatim, including leading dot
  segments. Normalizing them breaks file loading.
- **Inline comment anchoring.** Only path/side/line triples present in the diff
  can be posted. Comments are snapped to the nearest commentable line, and
  comments on paths outside the diff are dropped entirely. A finding that
  cannot be anchored belongs in the review body, not silently discarded.
- **Posting fallback.** GitHub may reject `APPROVE` or `REQUEST_CHANGES`
  depending on repository settings. The action falls back to `COMMENT` instead
  of failing, so generated and posted events differ by design. Do not
  "fix" that by assuming the generated event was published.
- **Chunk and character budgets.** Per-call caps, single-chunk caps, and the
  total corpus cap interact. Changing one without checking the others produces
  either oversized requests or spurious incomplete-coverage reports.

## 5. Keep the fix in scope

One logical change per commit. If you found unrelated problems while
debugging, note them in the PR description or open an issue. Do not smuggle a
refactor into a bug fix, it makes the fix unreviewable and unrevertable.

## 6. Verify

- [ ] A test reproduced the bug and now passes
- [ ] The regression test would have caught the original defect, not just the
      specific input you happened to try
- [ ] The root cause is fixed, or the special case is justified in writing
- [ ] Sibling call sites with the same shape were checked
- [ ] `python3 -m unittest` passes
- [ ] `README.md` updated if observable behavior changed
- [ ] The `review` skill has been run on the diff
