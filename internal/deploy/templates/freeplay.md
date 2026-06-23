---
name: freeplay
description: Use when a request has no more specific opening — the generic fact-first Arbiter loop.
max_steps: 64
---

[Verify] gear-up-published
# Build proof: asserts ONLY facts.published. The no-match filter makes test_run a no-op so the run
# is overall=errored (no_tests_ran); facts publish from src_compile BEFORE test_run and cannot be
# faked (libclang builds the index from the journaled TUs). Adding an "overall" clause would make
# this gate unsatisfiable — do not.
run: src_compile
tests: ["src_compile"]
expect: {"facts":{"published":true}}

[STEP] gear-up
[StepJob]
Choose the build profile from the request, then create one executor task that runs the
src_compile recipe and produces fresh facts before any implementation work starts.
[CheckList]
- Run the src_compile recipe with the selected profile (and any request-named feature flags) and confirm facts published
- Record the published snapshot or the typed reason publication failed
[Submit] gear-up-published
[Branch]
success: orient
failure: gear-up

[STEP] orient
[StepJob]
Use fact-first search and detail queries to identify the files, functions, tests, and
risks relevant to the request before dispatching edits.
[CheckList]
- Capture the fact_refs that should travel to executor tasks
- Explain the implementation scope in terms of facts or concrete source locations
[Branch]
success: plan
failure: gear-up

[STEP] plan
[StepJob]
Split the request into small executor tasks, attaching fact_refs to each task whose
context depends on facts.
[CheckList]
- Every task has a machine-checkable result predicate
- No task asks an executor to decide whether the overall request is done
[Branch]
success: execute
failure: orient

[STEP] execute
[StepJob]
Dispatch the planned tasks, review failed submissions, and re-dispatch only the work
needed to satisfy the predicates.
[CheckList]
- All open tasks have pass or fail verdicts
- Failures are either fixed by a follow-up task or routed to learn as a process failure
[Branch]
success: learn
failure: plan

[STEP] learn
[StepJob]
Record reusable gotchas from this run and finish with the referee-owned task and goal
evidence.
[CheckList]
- Useful step-scoped gotchas are added through NotePlaybook
- Final report cites task verdicts and verification evidence
[Branch]
success: END
failure: orient
