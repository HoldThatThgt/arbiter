---
name: hunt-latent-bugs
description: Use when asked to FIND defects nobody has pinned down yet - audit a module, review an area for correctness, or chase a vague suspicion. Produces machine-proven bugs - a compiled, running symptom-test plus a reachable trigger - never a list of style opinions. Do not use when the misbehavior is already known and reproducible (use fix-reported-bug) or for performance complaints (use fix-slow-path).
max_steps: 48
---

[Verify] symptom-proven
run: src_compile
tests: ["*"]
expect: {"overall":"passed"}
allow_overrides: ["tests"]

[STEP] hypothesize
[StepJob]
Study the code area named in the match request and form EXACTLY ONE falsifiable
bug hypothesis. It must name the file and line range, the failure mechanism
(which input or state makes which statement misbehave - data flow, lifetime,
boundary, concurrency), and expected vs actual behavior in one sentence each.
Style, naming, and comment complaints are NOT bugs: only observable misbehavior
counts. Touch no code in this step.

Anti-loop discipline (this step is re-entered after every disproof): before
filing, ListTask and skim prior hypotheses' summaries; ReviewTask any disproven
one and reuse what its failure exposed - often the same suspicion holds with a
different trigger or path. Never re-submit a disproven hypothesis unchanged.
Dead-end directions and environment quirks go into
NotePlaybook(step_id="hypothesize", note=...) - one line each - so future
rounds and future matches skip them.

One hypothesis per round, deliberately: the prove step adjudicates pass/fail
per round, and a round that bundles three hypotheses can only checkmate if all
three hold - weaker, slower, and it muddies which evidence proved what. File
the strongest; park the rest as one-line leads inside the task request.
[CheckList]
- Exactly one hypothesis naming a concrete file:line path
- Failure mechanism stated (trigger -> misbehaving statement)
- Falsifiable: a test could prove or disprove it mechanically
- Prior disproofs consulted; not a style/naming nit
[Branch]
success: prove
failure: hypothesize

[STEP] prove
[StepJob]
CreateTask for the executor (prefer the arbiter-debugger agent): turn the
hypothesis into a machine proof with a SYMPTOM test. The test must assert the
buggy behavior itself - it PASSES if and only if the bug exists:

  bug present  -> symptom assertion holds -> test passes (recipe overall=passed)
  bug absent   -> assertion fails        -> test fails  (recipe overall=failed)

so the proof is the curated predicate symptom-proven with tests overridden to
exactly the symptom test: the recipe builds, runs only that test, and certifies
overall=passed == "bug proven, mechanically". This polarity is the whole game: a
conventional regression test (assert CORRECT behavior, watch it fail) has the
opposite direction, and "it failed, see?" is exactly the prose claim the referee
refuses to take. Write the symptom test, never the regression form - the
regression form is what fix-reported-bug uses AFTER a bug is proven.

The test must not patch or work around the suspect code; a test that only passes
because it does not compile never reaches overall=passed (the build error is a
different verdict), so the proof cannot be faked. For corruption-class
hypotheses with gdb-mcp wired, strengthen the
proof: a watchpoint run (gdb_breakpoint kind=watch on the corrupted field)
stopping at exactly the predicted statement - capture the gdb_snapshot stop in
the task report alongside the test.

Judge the returned task: predicate passed for the predicted reason = proven,
branch success. Test could not be made to pass (hypothesis wrong) or passes for
an unrelated reason = disproven; record the disproof and branch failure.

Once the symptom test passes for the predicted reason, RegisterTest it to FREEZE
the proof: the referee re-hashes it before every verdict, so the machine-proven
symptom can never be quietly weakened or deleted after the fact.
[CheckList]
- Symptom test written (passes iff bug present); polarity stated in the task report
- Submit symptom-proven with tests overridden to the symptom test - it builds, runs green (bug proven)
- Proven symptom test RegisterTest-frozen so the proof cannot be weakened later
- Corruption-class: watchpoint stop evidence captured when gdb-mcp is wired
- Verdict recorded: proven for the predicted reason, or disproof noted
[Submit] symptom-proven
[Branch]
success: qualify
failure: hypothesize

[STEP] qualify
[StepJob]
Prove the bug matters before reporting it: document at least one concrete path
by which normal operation reaches the buggy code - a real caller chain, a
reachable input, or a configuration that triggers it. Dead code and
unreachable-in-practice findings do not qualify; branch failure and hunt
elsewhere (note the disqualification so the area is not re-hunted). Once fact
tools land (search/detail, milestone M4), ground reachability there instead of
manual tracing. Close with the final report: hypothesis, the symptom test
(path + passing predicate), the trigger path, and - if the user wants it
fixed - the recommendation to open a fix-reported-bug match seeded with this
symptom test inverted into a regression repro.
[CheckList]
- Concrete reachable trigger documented (caller chain or input)
- Report ties hypothesis + symptom test + trigger together
- Disqualified findings noted via NotePlaybook before re-hunting
[Branch]
success: END
failure: hypothesize
