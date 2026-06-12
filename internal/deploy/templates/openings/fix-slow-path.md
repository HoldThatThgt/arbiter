---
name: fix-slow-path
description: Use when code is reported slow, a performance regression appeared, or a hot path needs optimizing. Refuses to optimize without a reproducible measured baseline; checkmate is a measured improvement that beats the recorded noise band with the suite green and exactly one bounded change per round. Do not use for correctness problems (fix-reported-bug / hunt-latent-bugs) - "slow" here means measurably slow, not broken.
max_steps: 48
---

[STEP] scope
[StepJob]
Pin down WHAT is slow and HOW it will be observed - no code changes. Name the
code area from the request, then derive a deterministic workload: one argv
command (fixed inputs, fixed seeds, no network) that exercises the suspected
path and finishes in seconds-to-minutes. Record it verbatim - baseline and
prove-gain must run the IDENTICAL command or the comparison adjudicates
nothing.

CreateTask (prefer the arbiter-debugger agent) when perf-mcp is wired: run
perf.scan_c over the area and perf.toolchain_probe for the measurement options;
return the ranked findings (rule_id, file:line, severity, confidence). The
result predicate proves the scan really ran rather than being narrated:

  {"kind":"mcp","server":"perf-mcp","tool":"perf.scan_c",
   "arguments":{"paths":["<area>"],"min_severity":"low"},
   "expect":[{"path":"schema_version","op":"eq","value":"perf-mcp.scan.v1"},
             {"path":"summary","op":"exists"}]}

Zero findings is still a valid outcome - the workload then drives hotspot
discovery in baseline. Without perf-mcp, list candidate hotspots from reading
and say so in the report.
[CheckList]
- Slow path named with the observable complaint (latency, throughput, scaling)
- Deterministic workload command recorded verbatim (argv, fixed inputs/seeds)
- Scan findings captured via the expect-clause predicate, or "no static findings" recorded
[Branch]
success: baseline
failure: scope

[STEP] baseline
[StepJob]
Measure BEFORE touching anything. CreateTask: run the workload with
perf.measure_command, repeat >= 5, every run exiting 0, and submit the
measurement itself as the predicate - expect clauses give the mcp predicate
its teeth (a measurement whose runs failed cannot pass, no matter what the
text says):

  {"kind":"mcp","server":"perf-mcp","tool":"perf.measure_command",
   "arguments":{"command":["<argv0>","<argv1>","..."],"repeat":5},
   "expect":[{"path":"summary.all_successful","op":"eq","value":true},
             {"path":"summary.repeat","op":"ge","value":5}]}

Then measure a SECOND baseline the same way: the gap between the two medians
is the recorded noise band, and prove-gain must clear it - without the band,
"3% faster" is indistinguishable from measurement noise and the referee would
be rubber-stamping vibes. Two baselines that disagree wildly mean the workload
is not measurable: branch failure and rebuild it in scope. Without perf-mcp,
use hyperfine or /usr/bin/time via a shell predicate capturing the same
numbers. Both medians, the noise band, and the exact command go in the task
report.
[CheckList]
- Two baseline measurements, each >= 5 runs all exit 0 (expect-clause predicates passed)
- Noise band between the two medians written down
- Exact command + repeat + both medians stored in the task report
[Branch]
success: patch
failure: scope

[STEP] patch
[StepJob]
Pick exactly ONE finding or measured hotspot. When perf-mcp is wired, run
perf.explain_finding first and walk its false-positive checks - a finding that
fails them is discarded here with a recorded reason, not patched. CreateTask
for the fix: a minimal behavior-preserving change scoped to that finding, with
the result predicate proving the tree is still correct before any speed claim:

  {"kind":"shell","command":"git diff --quiet -- <test-paths> && <suite-command>"}

(test untouchability + suite green; speed is prove-gain's job, never this
step's). Bigger rewrites are out of scope by law: one bounded change per
round, measured before the next one - two unmeasured changes can mask each
other's regression and the match would adjudicate a lie.
[CheckList]
- Exactly one bounded change, tied to a named finding or measured hotspot
- False-positive checks consulted; discarded findings recorded with reasons
- Correctness predicate passed (zero test diff + suite green)
[Branch]
success: prove-gain
failure: patch

[STEP] prove-gain
[StepJob]
Re-measure with the IDENTICAL command and repeat count via the same
expect-clause measure predicate as baseline. Compare medians: the improvement
must exceed the recorded noise band. Improved beyond noise: write the final
report - finding -> change -> before/after medians -> band - and branch
success: checkmate. Within noise or regressed: revert the change, record what
was disproven via NotePlaybook(step_id="patch", note=...), and branch failure
to patch a different finding; when patch has no credible candidates left it
branches failure back to scope to re-derive the workload. A reverted attempt
with a recorded reason is a successful experiment, not a failure to hide.
[CheckList]
- Same command and repeat as baseline; after-median recorded via the measure predicate
- Improvement exceeds the recorded noise band; suite still green
- Report ties finding -> change -> before/after medians (or revert + disproof noted)
[Branch]
success: END
failure: patch
