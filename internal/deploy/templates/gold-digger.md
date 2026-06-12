---
name: gold-digger
description: Use when you must HUNT a bug whose symptom you can describe but whose failing test does not exist yet — a suspected crash, leak, race, or wrong result. A test-author writes a test that exposes it and FREEZES it; production code is then changed until that immutable test flips green with the suite intact. Do not use when a test already fails (use fix-reported-bug) or for slowness (use fix-slow-path).
max_steps: 64
verify_policy: named
---

[Verify] gear-up-published
run: src_compile
tests: ["src_compile"]
expect: {"overall":"passed","facts":{"published":true}}

[Verify] repro-fails
run: src_compile
tests: ["*"]
expect: {"overall":"failed"}
allow_overrides: ["tests"]

[Verify] suite-green
run: src_compile
tests: ["*"]
expect: {"overall":"passed","max_failed":0}

[STEP] gear-up
[StepJob]
Pick the build profile the symptom calls for — memory corruption/UAF/leak → asan, races → tsan
(when the recipe has it, else debug), otherwise debug — and publish fresh facts through src_compile
under that profile before touching source, so the hunt is grounded in a typed index.
[CheckList]
- Submit gear-up-published with the profile the symptom calls for
- Record the snapshot id or the typed publication failure
[Submit] gear-up-published
[Branch]
success: reproduce
failure: gear-up

[STEP] reproduce
[StepJob]
Dispatch the arbiter-test-author with the symptom as a SCENARIO (what is wrong, when, any captured
signature), never how to write the test. It hunts the bug down to a deterministic gtest that
asserts the CORRECT behavior — so it FAILS while the bug is live — proves it runs red through
src_compile, and RegisterTest-freezes it. From that instant the reproduction is immutable.
[CheckList]
- The test-author owns the test; the dispatch carries the symptom, not an implementation
- Submit repro-fails with tests overridden to the new test — it runs and fails (bug exposed)
- The repro is RegisterTest-frozen before finishing; zero production code touched
[Submit] repro-fails
[Branch]
success: fix
failure: reproduce

[STEP] fix
[StepJob]
Dispatch the arbiter-debugger first when the symptom is a crash/UAF/race and gdb-mcp is wired (read
the runtime state at the fault), else the arbiter-implementer. It changes PRODUCT code only — the
repro is frozen — until the frozen repro flips green and the whole suite passes. A failed attempt
loops here; you cannot make the test pass by touching it.
[CheckList]
- Fix confined to product code; the frozen repro is untouched (enforced by the freeze)
- Submit suite-green — the full suite, including the now-green repro, passes
[Submit] suite-green
[Branch]
success: END
failure: fix
