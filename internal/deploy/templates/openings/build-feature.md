---
name: build-feature
description: Use when building NEW functionality whose shape the user can describe - scenario-first TDD walked under the referee. User-confirmed scenarios become approved failing tests, then a make-it-pass implementation, then a refactor to module conventions; the tests are untouchable and the referee checks that mechanically at every step. Do not use for bug work (fix-reported-bug / hunt-latent-bugs) or pure refactoring with no new behavior.
max_steps: 64
verify_policy: named
---

[Verify] tests-fail
run: src_compile
tests: ["*"]
expect: {"overall":"failed"}
allow_overrides: ["tests"]

[Verify] suite-green
run: src_compile
tests: ["*"]
expect: {"overall":"passed","max_failed":0}

[STEP] scenario
[StepJob]
Interview the user until you can write 1-2 MAIN success scenarios: given which
concrete input or action, the user observes exactly which output or state
change. Externally observable behavior only, in the user's own words - no
design talk, no implementation nouns. Echo the final wording back. The scenarios
become the contract every later predicate enforces, so vague wording here is
debt every step downstream pays. This step is a checkpoint: only the user's
explicit yes advances it - put the echoed scenarios to them and relay their
choice.
[Checkpoint]
Do these 1-2 scenarios capture the feature you want built, in your own words
(concrete input -> observable outcome, no implementation detail)? Approve to
proceed to writing the tests; reject to revise the wording.
[Branch]
success: testcase
failure: scenario

[STEP] testcase
[StepJob]
Dispatch the arbiter-test-author with the confirmed scenarios AS SCENARIOS - the
input and the observable outcome, never how to write the test. It independently
turns each into a test case that compiles and runs NOW, and fails NOW solely
because the feature is missing, then records the exact scenario-test run command
in the task report (every later step reuses it verbatim).

The proof is the curated predicate tests-fail with tests overridden to exactly
the new scenario tests: the recipe builds, runs only those tests, and certifies
overall=failed - build green AND the new tests red, the TDD starting position.
A test that "fails" because it does not compile never reaches overall=failed
(the build error is a different verdict), so the polarity cannot be faked.

When it comes back, show the user the test code and the failing output and get
explicit approval - the user's yes, not the predicate, is what makes these the
contract. Rejected tests or a wrong failure reason: branch failure, back to
scenario. Once the user approves, the test-author RegisterTest-freezes the test
file(s). From that instant no one can modify the approved tests by any means -
the referee re-hashes them before every verdict and the guard denies edits - so
the implementation must satisfy the tests as approved.
[CheckList]
- The test-author owns the test; the dispatch carried the scenarios, not an implementation
- Submit tests-fail with tests overridden to the new scenario tests - build green, new tests red
- Failure output shows the feature-missing reason, not a compile error or test bug
- User saw the test code and explicitly approved it
- Approved test file(s) RegisterTest-frozen before advancing; scenario-test run command recorded
[Submit] tests-fail
[Branch]
success: hack
failure: scenario

[STEP] hack
[StepJob]
Dispatch the arbiter-implementer: make the approved tests pass by ANY means -
hacks, hardcoding, shortcuts all welcome - changing PRODUCT code only. The test
files are frozen, so a pass can come from nowhere but the implementation; the
referee re-hashes them before the verdict, which is why the predicate no longer
needs to police the diff itself. Submit suite-green: the recipe runs the whole
suite, including the now-frozen scenario tests, and certifies overall=passed with
zero failures. Stop the moment it passes; polish belongs to the next step.
[CheckList]
- Submit suite-green - scenario tests and the full suite pass, frozen tests untouched
- Change confined to product code; implementation stopped at first green (no premature polish)
[Submit] suite-green
[Branch]
success: elevate
failure: hack

[STEP] elevate
[StepJob]
Dispatch the arbiter-implementer to raise the hack to production quality without
breaking anything. First relocate the code to its rightful module (not the
bolt-on spot the hack used). Then read that module's existing code and align with
its actual conventions - for C-style modules: ownership and init/free pairing,
error-code style, opaque structs, vtable/function-pointer dispatch, naming.
Submit suite-green after every move; it stays the gate (frozen tests untouched,
whole suite green). When perf-mcp is wired, finish with perf.scan_c over exactly
the files this feature touched: every NEW high-severity finding gets fixed here
or justified in one line (cold path, bounded size) - pre-existing findings are
out of scope.
[CheckList]
- Code relocated to its rightful module; module's pre-existing conventions identified and followed
- Submit suite-green - frozen scenario tests and the full suite still pass after the refactor
- Perf scan of touched files reviewed when perf-mcp is wired; new high findings fixed or justified
[Submit] suite-green
[Branch]
success: END
failure: elevate
