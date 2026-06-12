---
name: fix-reported-bug
description: Use when a KNOWN misbehavior must be eliminated - a reported crash, wrong result, or flaky symptom you can describe. A test-author writes a test that reproduces the symptom and FREEZES it; production code is then changed until that exact immutable test flips to green with the suite intact. Do not use to LOOK for unknown bugs (use hunt-latent-bugs) or for slowness (use fix-slow-path).
max_steps: 64
verify_policy: named
---

[Verify] repro-runs-red
run: primary
tests: ["*"]
expect: {"overall":"failed"}
allow_overrides: ["tests"]

[Verify] suite-green
run: primary
tests: ["*"]
expect: {"overall":"passed","max_failed":0}

[SetGoal]
verify: suite-green

[STEP] write-repro
[StepJob]
Dispatch the arbiter-test-author with the bug as a SCENARIO — the symptom, trigger, and any
captured signature, never how to write the test. It independently writes a deterministic test that
asserts the CORRECT behavior, so the test FAILS while the bug exists (run-red standard), proves it
runs red through the recipe, and then RegisterTest-freezes it. From that instant the reproduction is
immutable — the fix cannot weaken, delete, or rewrite it. Do not touch production code in this step.
[CheckList]
- The test-author owns the test; the dispatch carries the scenario, not an implementation
- Submit repro-runs-red with tests overridden to the new test — it runs and fails (bug reproduced)
- The repro is RegisterTest-frozen before finishing; zero production code touched
[Submit] repro-runs-red
[Branch]
success: fix
failure: write-repro

[STEP] fix
[StepJob]
Dispatch the arbiter-implementer (arbiter-debugger first when the symptom is a crash/UAF/race and
gdb-mcp is wired, to localize from runtime state — read locals at the offending write, not code
alone). It changes PRODUCT code only — the repro test is frozen, so a fix can come from nowhere
else — until the frozen repro flips to green and the whole suite passes. A failed attempt loops
here: revert and re-localize. You cannot make the test pass by touching it; the referee re-hashes it
before the verdict.
[CheckList]
- Fix confined to product code; the frozen repro is untouched (enforced by the freeze, not trusted)
- Submit suite-green — the full suite, including the now-green repro, passes
[Submit] suite-green
[Branch]
success: END
failure: fix
