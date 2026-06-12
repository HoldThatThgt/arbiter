---
name: fix-slow-path
description: Use when code is reported slow, a performance regression appeared, or a hot path needs optimizing. A test-author writes a deterministic COMPLEXITY-RATIO test - the workload at size N and 2N, asserting the growth ratio stays under a fixed bound - and FREEZES it; production code is then optimized until that immutable test flips from red to green with the suite intact. The ratio proves the algorithmic fix and is robust to host speed; perf-mcp is analysis only - it finds WHERE to optimize, never proof. Do not use for correctness problems (fix-reported-bug / hunt-latent-bugs) - "slow" here means measurably slow, not broken.
max_steps: 48
verify_policy: named
---

[Verify] ratio-runs-red
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

[STEP] write-ratio-test
[StepJob]
Hand the arbiter-test-author the SCENARIO and the standard, never an implementation. The
scenario: the user-facing operation that is slow, the input-size knob (what "N" is), the
observable cost (latency / throughput / scaling), and the suspected growth ("should be
~linear; today doubling N more than doubles the time"). The standard this opening proves
is a COMPLEXITY-RATIO test — run the same workload at N and 2N and assert the growth ratio
time(2N)/time(N) stays under a fixed bound. A ratio proves the ALGORITHMIC fix and
survives a fast or slow host; an absolute wall-time budget does not. HOW it makes that
robust — median of repeats, an N in the asymptotic regime, a bound K with margin between
linear ≈ 2 and quadratic ≈ 4 — is the test-author's call, not yours.

It writes the test, proves it runs RED today (the slow path violates the bound) through
the recipe, then RegisterTest-freezes it — from that instant the requirement is immutable.
No production code in this step. perf-mcp is not needed here: the test is black-box over
the workload; the tools earn their keep next, finding where the time goes.
[CheckList]
- The test-author owns the test; the dispatch carries the scenario, not an implementation
- The test asserts a GROWTH RATIO across N and 2N (robust to host speed), never an absolute time
- Submit ratio-runs-red with tests overridden to the new test — it runs and fails (slowness reproduced)
- The ratio test is RegisterTest-frozen before finishing; zero production code touched
[Submit] ratio-runs-red
[Branch]
success: optimize
failure: write-ratio-test

[STEP] optimize
[StepJob]
Dispatch the arbiter-debugger — it OBSERVES where the time goes. Reach for perf.scan_c to
rank suspect sites, perf.explain_finding to vet one before touching it, and — only when
choosing between candidate changes — perf.measure_command (argv arrays, repeat ≥ 5, a
second baseline for the noise band) to see which is worth trying. When a hotspot emerges,
ground it: search {callers:<fn>} / {reachable:<entry>-><fn>} confirms the path is actually
reached, so you don't optimize dead code. These tools only point at WHERE and WHICH to
try — keeping or reverting a change is decided by ONE thing: whether the frozen ratio test
moves toward green (the debugger agent's one rule). Stop measuring once you have a change
to make; without perf-mcp, read the hot path, apply the obvious win, and go.

Change PRODUCT code only — the ratio test is frozen and re-hashed before the verdict, so
the fix can come from nowhere else — one bounded change per round. A change that doesn't
move the ratio test toward green is reverted with a recorded reason via
NotePlaybook(step_id="optimize", note=...) (a disproven experiment is a result), and you
pivot to the next hotspot rather than grinding the same one; if nothing moves it, report
which sites you tried and why — the slowness may be algorithmic by design. You cannot make
the test pass by touching it.
[CheckList]
- Optimization confined to product code; the frozen ratio test untouched (enforced by the freeze, not trusted)
- perf / facts evidence (chosen hotspot, before/after medians) in the report — as analysis, never as the proof
- Submit suite-green — the frozen ratio test now passes and the full suite is green
[Submit] suite-green
[Branch]
success: END
failure: optimize
