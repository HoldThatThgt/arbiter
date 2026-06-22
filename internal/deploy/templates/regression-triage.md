---
name: regression-triage
description: Use when a fresh regression must be reduced to a failing test and a verified fix — something that worked before now misbehaves. A test-author pins the regression in a test and FREEZES it; production code is changed until that immutable test flips green with the suite intact. Do not use for never-worked features (use build-feature) or unknown defects (use hunt-latent-bugs).
max_steps: 64
verify_policy: named
---

[Verify] gear-up-published
# Build proof: asserts ONLY facts.published. The no-match filter makes test_run a no-op so the run
# is overall=errored (no_tests_ran); facts publish from src_compile BEFORE test_run and cannot be
# faked (libclang builds the index from the journaled TUs). Adding an "overall" clause would make
# this gate unsatisfiable — do not.
run: src_compile
tests: ["src_compile"]
expect: {"facts":{"published":true}}

[Verify] regression-reproduced
run: src_compile
tests: ["*"]
expect: {"overall":"failed"}
allow_overrides: ["tests"]

[Verify] suite-green
run: src_compile
tests: ["*"]
expect: {"overall":"passed","max_failed":0}

[SetGoal]
verify: suite-green

[STEP] gear-up
[StepJob]
Publish fresh facts through src_compile with the profile the regression report implies, before
touching source, so triage is grounded in a typed index.
[CheckList]
- Submit gear-up-published with any request-named feature flags
- Record the snapshot id before dispatching triage
[Submit] gear-up-published
[Branch]
success: reproduce
failure: gear-up

[STEP] reproduce
[StepJob]
Dispatch the arbiter-test-author with the regression as a SCENARIO (what worked before, what
misbehaves now, since when), never how to write the test. It pins the regression in a deterministic
test that asserts the CORRECT (pre-regression) behavior — so it FAILS now — proves it runs red
through src_compile, and RegisterTest-freezes it.
[CheckList]
- The test-author owns the test; the dispatch carries the regression, not an implementation
- Submit regression-reproduced with tests overridden to the new test — it runs and fails
- The test is RegisterTest-frozen before finishing; zero production code touched
[Submit] regression-reproduced
[Branch]
success: fix
failure: reproduce

[STEP] fix
[StepJob]
Dispatch the arbiter-implementer (arbiter-debugger first if the regression is a crash/UAF/race and
gdb-mcp is wired). Ground the suspect change in facts (search/detail for callers and writers of the
implicated state), then change PRODUCT code only — the test is frozen — until the frozen regression
test flips green and the whole suite passes. A failed attempt loops here.
[CheckList]
- Fix confined to product code; the frozen test is untouched (enforced by the freeze)
- Submit suite-green — the full suite, including the now-green regression test, passes
[Submit] suite-green
[Branch]
success: END
failure: fix
