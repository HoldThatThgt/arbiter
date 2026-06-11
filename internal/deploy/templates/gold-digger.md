---
name: gold-digger
description: Bug-hunt opening that proves the repro, fixes it, then proves the repro is gone.
max_steps: 64
---

[Verify] gear-up-published
run: src_compile
tests: ["src_compile"]
expect: {"overall":"passed","facts":{"published":true}}

[Verify] repro-fails
run: primary
tests: ["Bug.Repro"]
expect: {"overall":"failed","test":{"name":"Bug.Repro","result":"failed"}}

[Verify] repro-passes
run: primary
tests: ["Bug.Repro"]
expect: {"overall":"passed","max_failed":0}

[SetGoal]
run: primary
tests: ["*"]
expect: {"overall":"passed","max_failed":0}

[STEP] gear-up
[StepJob]
Select the profile from the request and publish fresh facts through the src_compile recipe before
touching source.
[CheckList]
- Submit gear-up-published with the selected profile and any feature flags from the request
- Record the snapshot id or the typed publication failure
[Branch]
success: reproduce
failure: gear-up

[STEP] reproduce
[StepJob]
Narrow the reported bug to a failing gtest and prove the failure with structured output only.
[CheckList]
- Submit repro-fails for the smallest available reproducer
- Attach fact_refs for code regions that explain why the failure is plausible
[Branch]
success: fix
failure: gear-up

[STEP] fix
[StepJob]
Dispatch the smallest fact-informed source change that can make the proven reproducer pass.
[CheckList]
- Executor tasks include fact_refs for touched functions or fields
- No task asks the executor to decide whether the bug is fixed
[Branch]
success: verify
failure: reproduce

[STEP] verify
[StepJob]
Run the reproducer and the primary suite predicate; use failures to create narrower follow-up
tasks.
[CheckList]
- Submit repro-passes
- Report any remaining failed test as a new task with machine-checkable evidence
[Branch]
success: learn
failure: fix

[STEP] learn
[StepJob]
Record reusable gotchas and finish with referee-owned evidence.
[CheckList]
- Add step-scoped gotchas through NotePlaybook when the hunt exposed a reusable trap
- Final report cites the failing-before and passing-after run evidence
[Branch]
success: END
failure: verify
