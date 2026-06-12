---
name: review
description: Code-review bug hunt that proves one concrete bug with a failing test and shows real code reaches it.
max_steps: 48
verify_policy: open
---

[Verify] bug-proven
run: primary
tests: ["*"]
expect: {"overall":"failed"}
allow_overrides: ["tests"]

[STEP] inspect
[StepJob]
Plan-only step: study the target code through the player seat's search and detail MCP
tools — search to sweep candidate symbols, detail to read each suspect's typed record —
and propose exactly ONE concrete bug hypothesis, stated as machine-checkable behavior.
Create the test-writing task with CreateTask, attaching the fact_refs that search and
detail returned for every symbol the hypothesis names. No code edits in this step.
[CheckList]
- The hypothesis names the suspect symbol, the wrong behavior, and the concrete input that triggers it
- The task carries fact_refs from search/detail for the named symbols
- No file was edited during this step
[Branch]
success: prove
failure: inspect

[STEP] prove
[StepJob]
Dispatch the arbiter-test-author subagent to write the minimal test that fails if and
only if the bug exists. The subagent proves the failure through the executor seat's run
MCP tool (recipe primary) and submits via SubmitTask with result
{"verify": "bug-proven", "tests": [<new test name>]} — the tests override pins the
candidate test so the verdict comes from referee-owned run output, never prose. A
passing test means the hypothesis was wrong: take the failure branch and hunt again,
never weaken the test to force a failure.
[CheckList]
- arbiter-test-author wrote one minimal test and no non-test source changed
- bug-proven was submitted with the new test name in the tests override
- The failing run evidence is referee-owned run output, not prose
[Branch]
success: impact
failure: inspect

[STEP] impact
[StepJob]
Prove existing code actually hits the bug. Use the player seat's search tool with
relation queries (callers, reachability) anchored on the buggy symbol, then create a
verification task whose executor submits an inline fact predicate as the evidence — for
example a callers query with expect {"min_results":1}, or a reachability query with
expect {"reachable":true}. If no caller or path reaches the buggy symbol the finding has
no real-world impact: take the failure branch and hunt for a different bug.
[CheckList]
- A fact predicate anchored on the buggy symbol passed (min_results or reachable form)
- The final report links the failing test, the passing fact evidence, and the hypothesis
[Branch]
success: END
failure: inspect
