---
name: feature
description: Scenario-driven feature work that goes red tests first, then green by any means, then elevates the code into its rightful module.
max_steps: 64
verify_policy: open
---

[Verify] feature-red
run: primary
tests: ["*"]
expect: {"overall":"failed"}
allow_overrides: ["tests"]

[Verify] feature-green
run: primary
tests: ["*"]
expect: {"overall":"passed"}
allow_overrides: ["tests"]

[Verify] suite-green
run: primary
tests: ["*"]
expect: {"overall":"passed","max_failed":0}

[STEP] scenarios
[StepJob]
Plan-only step: keep asking the USER until one or two concrete main success scenarios
exist, each stated as input → observable outcome, and the user has explicitly confirmed
them. Orient with the player seat's search and detail MCP tools so you know which
modules the feature touches, then create the test-writing task with CreateTask, writing
the confirmed scenarios verbatim into the task request and attaching the fact_refs from
search/detail. No code in this step.
[CheckList]
- One or two scenarios, each with a concrete input and an observable outcome
- The user explicitly confirmed the scenarios
- Scenarios appear verbatim in the task request, with fact_refs attached
[Branch]
success: red-tests
failure: scenarios

[STEP] red-tests
[StepJob]
Dispatch the arbiter-test-author subagent to transform the scenarios into test cases
that COMPILE AND RUN but FAIL because the feature is absent — prove this through the
executor seat's run MCP tool (recipe primary); a compile error is not a red test.
Checkpoint: present the test code to the user and get explicit approval BEFORE
submitting. Then submit via SubmitTask with result
{"verify": "feature-red", "tests": [<new test names>]}.
[CheckList]
- New tests compile and run, and fail only because the feature is missing
- The user approved the test code before submission
- feature-red was submitted with the new test names in the tests override
[Branch]
success: make-green
failure: scenarios

[STEP] make-green
[StepJob]
Dispatch the arbiter-implementer subagent to make the red tests pass — any hack is
welcome at this step. The implementer may run tests (executor seat run MCP tool, never
raw shell where the run tool fits) and read test code, but must never modify tests; the
referee checks this structurally, not by trust. Submit via SubmitTask with result
{"verify": "feature-green", "tests": [<new test names>]}, plus a second submission
carrying an inline shell predicate `git diff --exit-code -- <test paths>` that proves
the tests are untouched. Pin the exact test file paths into that predicate.
[CheckList]
- feature-green passed with the new test names in the tests override
- An inline shell predicate git diff --exit-code -- <exact test file paths> passed (pin the real paths the tests live in, not a glob guess)
- The implementer cited fact_refs for the code it changed
[Branch]
success: elevate
failure: red-tests
[Gotcha]
- failure routes to red-tests, not a make-green self-loop: if the implementer repeatedly cannot go green, suspect the tests (wrong expectations, hidden environment coupling) before suspecting the implementation.

[STEP] elevate
[StepJob]
Dispatch the arbiter-implementer subagent to relocate the working code to its rightful
module: first inspect that module's existing design pattern through the executor seat's
search and detail MCP tools, then refactor the new code to align with it. Submit via
SubmitTask with result {"verify": "suite-green"}, plus the tests-untouched inline shell
predicate `git diff --exit-code -- <test paths>` again.
[CheckList]
- The code lives in its rightful module and follows that module's existing pattern, with fact_refs cited as evidence
- suite-green passed
- The tests-untouched shell predicate passed again with the same pinned paths
[Branch]
success: END
failure: make-green
