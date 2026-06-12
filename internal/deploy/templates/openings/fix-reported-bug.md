---
name: fix-reported-bug
description: Use when a KNOWN misbehavior must be eliminated - a reported crash, wrong result, or flaky symptom you can describe. The match refuses to touch production code until a test reproduces the symptom on every run; the fix is only accepted while that exact unmodified test flips to green with the suite intact. Do not use to LOOK for unknown bugs (use hunt-latent-bugs) or for slowness (use fix-slow-path).
max_steps: 64
---

[STEP] design-repro
[StepJob]
Design a reproduction - do NOT attempt any fix and do NOT touch production code.
Work out the exact trigger (inputs, state, environment, ordering) and specify a
test that MUST fail on every run while the bug exists. Name each determinism
hazard you can see - timing, randomness, map/iteration order, filesystem state,
parallelism - and write down how the test pins it (fixed seed, single thread,
controlled clock, scratch dir). Decide the exact command that will run ONLY
this test (you will reuse the same command verbatim in every later step; record
it in the design).

Why the referee needs this step: every later verdict in this match is anchored
to one immutable artifact - the repro test and its run command. A fix that
cannot point at "this exact command flipped from deterministic-fail to
deterministic-pass" does not adjudicate, no matter how convincing the diff
looks.
[CheckList]
- Trigger conditions written down (inputs, state, environment, ordering)
- Every named determinism hazard has a written pin
- The exact single-test run command is recorded
- Zero production code touched
[Branch]
success: prove-repro
failure: design-repro

[STEP] prove-repro
[StepJob]
CreateTask for the executor (dispatch to the arbiter-debugger agent when it
exists, else arbiter-executor): implement the designed test, then PROVE the
reproduction is deterministic. The task request must order the executor to
submit this result predicate - 5 consecutive runs, every one failing:

  {"kind":"shell","command":
   "i=0; while [ $i -lt 5 ]; do if <run-command>; then exit 1; fi; i=$((i+1)); done"}

Polarity note (this is what makes the step adjudicable): while the bug exists
the test FAILS, so the predicate succeeds only when all 5 runs fail - exit 0
here means "reproduction proven", and no prose can substitute for it. If any
run passes, the predicate exits 1 and the referee records a failed task.

When the symptom is a crash or memory corruption and gdb-mcp is wired, the same
task must also capture the structured crash signature once: gdb_start the test
binary, gdb_exec run, gdb_snapshot at the stop - stop reason plus top frames go
into the task report. "Same symptom" in later steps means same signature, never
similar-looking log text.
[CheckList]
- Repro test implemented; the 5x all-fail shell predicate passed
- Crash-class symptoms: GDB stop signature captured in the task report (when wired)
- Adding the test left the rest of the suite untouched
[Branch]
success: localize
failure: design-repro

[STEP] localize
[StepJob]
Form ONE root-cause hypothesis grounded in observed runtime state, not in code
reading alone. Dispatch the arbiter-debugger: run the repro under GDB with a
breakpoint on the implicated path - or a watchpoint (gdb_breakpoint kind=watch)
on the corrupted variable to catch the offending write - and read locals/args
at the stop (gdb_snapshot, gdb_eval). The hypothesis must explain the concrete
values observed (task report carries them). Then state: the misbehaving
statement(s), why they produce exactly this symptom, and a minimal fix sketch
scoped to the directly involved files. If a previous fix attempt failed, read
its task via ReviewTask first and write one line on what it disproved - never
re-submit a disproven hypothesis unchanged.
[CheckList]
- Hypothesis names specific statements and explains the observed values
- GDB stop/watchpoint evidence attached when gdb-mcp is wired
- Minimal fix sketch scoped to directly involved files
- Prior failed attempts accounted for (ReviewTask consulted)
[Branch]
success: fix
failure: localize

[STEP] fix
[StepJob]
CreateTask for the executor: apply the sketched fix - nothing beyond the
hypothesized scope - then submit a result predicate that proves all three laws
at once: the repro now passes 5x, the repro test file was never modified, and
the full suite is green:

  {"kind":"shell","command":
   "git diff --quiet -- <repro-test-path> && i=0; while [ $i -lt 5 ]; do <run-command> || exit 1; i=$((i+1)); done && <suite-command>"}

The `git diff --quiet` clause is the untouchability law as a machine check: an
executor that "fixed" the bug by editing the repro test fails the predicate
mechanically - the referee never has to trust a claim that the test is intact.
On any predicate failure, branch failure into triage; do not retry blindly.
[CheckList]
- Fix confined to the hypothesized scope (diff reviewed)
- Combined predicate passed: repro 5x green + repro file unmodified + suite green
[Branch]
success: END
failure: triage

[STEP] triage
[StepJob]
Classify the failed attempt by re-running the UNMODIFIED repro 5x against the
post-attempt tree (same shell-loop predicate as prove-repro, dispatched as a
task). Deterministic fail every run = the reproduction is intact and only the
hypothesis was wrong - revert the attempt, branch success back to localize.
Mixed pass/fail or a different failure signature (compare the GDB signature,
not log text) = the attempt corrupted the reproduction - revert, note what it
changed, branch failure to design-repro. Record the dead end with
NotePlaybook(step_id="localize", note=...) so the next round does not repeat it.
[CheckList]
- 5x post-attempt predicate run recorded as a task
- Outcome classified by signature: hypothesis-wrong vs repro-broken
- Attempt reverted; dead end noted via NotePlaybook
[Branch]
success: localize
failure: design-repro
