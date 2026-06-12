---
name: build-feature
description: Use when building NEW functionality whose shape the user can describe - scenario-first TDD walked under the referee. User-confirmed scenarios become approved failing tests, then a make-it-pass implementation, then a refactor to module conventions; the tests are untouchable and the referee checks that mechanically at every step. Do not use for bug work (fix-reported-bug / hunt-latent-bugs) or pure refactoring with no new behavior.
max_steps: 64
---

[STEP] scenario
[StepJob]
Interview the user until you can write 1-2 MAIN success scenarios: given which
concrete input or action, the user observes exactly which output or state
change. Externally observable behavior only, in the user's own words - no
design talk, no implementation nouns. Echo the final wording back and get an
explicit yes; that confirmation is this step's only deliverable. The scenarios
become the contract every later predicate enforces, so vague wording here is
debt every step downstream pays.
[CheckList]
- 1-2 scenarios, each with concrete input and observable outcome
- No implementation details inside the scenario wording
- User explicitly confirmed the exact wording
[Branch]
success: testcase
failure: scenario

[STEP] testcase
[StepJob]
CreateTask for the executor: turn the confirmed scenarios into test cases that
compile and run NOW, and fail NOW solely because the feature is missing. Record
the exact scenario-test run command in the task (every later step reuses it
verbatim). The result predicate proves both halves mechanically -
build succeeds AND the new tests fail:

  {"kind":"shell","command":"<build-command> && ! <scenario-test-run-command>"}

The `&& !` polarity matters: a test that fails because it does not compile, or
because of a typo, satisfies a naive "it fails" check - this predicate only
passes when the build is green and the run is red, which is the TDD starting
position the referee can actually certify.

When the result comes back, show the user the test code and the failing output,
and get explicit approval. The user's yes - not the predicate - is this step's
gate; the predicate only guarantees what they approved is real. Rejected tests
or a wrong failure reason: branch failure, back to scenario.

Once the user approves, RegisterTest the test file(s) to FREEZE them. From that
instant no one can modify the approved tests by any means - the referee re-hashes
them before every verdict and the guard denies edits - so the implementation must
satisfy the tests as approved, and the untouchability law below is enforced by
content hash, not just by diff.
[CheckList]
- Tests compile; predicate "build && ! run" passed
- Failure output shows the feature-missing reason, not a test bug
- User saw the test code and explicitly approved it
- Approved test file(s) RegisterTest-frozen before advancing
- Scenario-test run command recorded for later steps
[Branch]
success: hack
failure: scenario

[STEP] hack
[StepJob]
CreateTask for the executor: make the approved tests pass by ANY means - hacks,
hardcoding, shortcuts all welcome. One law, enforced mechanically: test files
may be run and read, never modified, deleted, skipped, or annotated. The result
predicate certifies green-plus-untouched in one shot:

  {"kind":"shell","command":
   "git diff --quiet -- <test-paths> && <scenario-test-run-command> && <suite-command>"}

`git diff --quiet` is the untouchability law as a machine check - an executor
that bent a test to pass fails the predicate before any human reads the diff.
Stop the moment the predicate passes; polish belongs to the next step.
[CheckList]
- Combined predicate passed: zero test diff + scenario tests green + suite green
- Implementation stopped at first green (no premature polish)
[Branch]
success: elevate
failure: hack

[STEP] elevate
[StepJob]
Raise the hack to production quality without breaking anything. First relocate
the code to its rightful module (not the bolt-on spot the hack used). Then read
that module's existing code and align with its actual conventions - for C-style
modules: ownership and init/free pairing, error-code style, opaque structs,
vtable/function-pointer dispatch, naming. Re-run the same combined predicate
from hack after every move; it remains the gate (tests still untouchable, suite
still green). When perf-mcp is wired, finish with perf.scan_c over exactly the
files this feature touched: every NEW high-severity finding gets fixed here or
justified in one line (cold path, bounded size) - pre-existing findings are out
of scope. The closing task's result predicate is the same combined check from
hack, re-submitted against the final tree.
[CheckList]
- Code relocated to its rightful module
- Module's pre-existing conventions identified and followed
- Final combined predicate passed (zero test diff + scenario + suite green)
- Perf scan of touched files reviewed when perf-mcp is wired; new high findings fixed or justified
[Branch]
success: END
failure: elevate
