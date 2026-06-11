---
name: regression-triage
description: Opening for reducing a fresh regression to a failing test and a verified fix path.
max_steps: 64
---

[Verify] gear-up-published
run: src_compile
tests: ["src_compile"]
expect: {"overall":"passed","facts":{"published":true}}

[Verify] regression-reproduced
run: primary
tests: ["Regression.*"]
expect: {"overall":"failed","min_passed":0}

[Verify] suite-green
run: primary
tests: ["*"]
expect: {"overall":"passed","max_failed":0}

[SetGoal]
run: primary
tests: ["*"]
expect: {"overall":"passed","max_failed":0}

[STEP] gear-up
[StepJob]
Publish fresh facts with the profile implied by the regression report.
[CheckList]
- Submit gear-up-published with request-named feature flags
- Record the snapshot id before dispatching triage tasks
[Branch]
success: reproduce
failure: gear-up

[STEP] reproduce
[StepJob]
Reproduce the regression and capture the smallest failing test or test filter.
[CheckList]
- Submit regression-reproduced when a failing filter is available
- If reproduction is environment-specific, capture the typed run failure rather than prose
[Branch]
success: localize
failure: gear-up

[STEP] localize
[StepJob]
Use facts and run history to bound the suspect files, call paths, or recent changes before edits.
[CheckList]
- Attach fact_refs for suspect entry points or state mutations
- Create one executor task per independently testable hypothesis
[Branch]
success: fix
failure: reproduce

[STEP] fix
[StepJob]
Apply the smallest correction and rerun the failing filter before the wider suite.
[CheckList]
- Executor submissions include the failing filter evidence
- Follow-up tasks are narrower than the failed hypothesis
[Branch]
success: verify
failure: localize

[STEP] verify
[StepJob]
Prove the regression is gone and the primary suite is green.
[CheckList]
- Submit suite-green
- Report residual failures as separate triage tasks
[Branch]
success: END
failure: fix
