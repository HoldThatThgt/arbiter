---
name: debug
description: Reproducible-bug fixing that pins a deterministic repro test before any fix is attempted.
max_steps: 64
verify_policy: open
---

[Verify] repro-fails
run: primary
tests: ["*"]
expect: {"overall":"failed"}
allow_overrides: ["tests"]

[Verify] repro-passes
run: primary
tests: ["*"]
expect: {"overall":"passed","max_failed":0}
allow_overrides: ["tests"]

[STEP] design-repro
[StepJob]
Plan-only step: design the test that makes the problem ALWAYS reproducible — a
deterministic trigger, no sleeps, retries, or timing dependence. Orient through the
player seat's search and detail MCP tools to find the entry points and state the bug
flows through, then create the repro task with CreateTask, attaching the fact_refs from
search/detail for every symbol the design names. No code edits in this step.
[CheckList]
- The design names the deterministic trigger and the observable wrong outcome
- The design contains no sleeps, retries, or timing assumptions
- The repro task carries fact_refs from search/detail
[Branch]
success: prove-repro
failure: design-repro

[STEP] prove-repro
[StepJob]
Dispatch the arbiter-test-author subagent to write the repro test and prove it reliably
reproduces: run it repeatedly through the executor seat's run MCP tool — use the
runner's repeat options through the run tool when the recipe supports them, otherwise
several consecutive runs — and every run must fail. Submit via SubmitTask with result
{"verify": "repro-fails", "tests": [<repro test name>]} so the failing evidence is
referee-owned run output.
[CheckList]
- The repro test failed on every repeated run, not just once
- repro-fails was submitted with the repro test name in the tests override
- No non-test source changed
[Branch]
success: hypothesize
failure: design-repro

[STEP] hypothesize
[StepJob]
Plan-only step: inspect the code around the failure through the player seat's search and
detail MCP tools, including relation queries (callers, callees, reachability), and form
exactly ONE fix hypothesis. Create the fix task with CreateTask, attaching fact_refs for
the functions and fields the hypothesis blames. No code edits in this step.
[CheckList]
- Exactly one hypothesis, naming the defect site and the mechanism that produces the failure
- The fix task carries fact_refs from search/detail
[Branch]
success: fix
failure: design-repro

[STEP] fix
[StepJob]
Dispatch the arbiter-implementer subagent to implement the fix. It may run tests
(executor seat run MCP tool) and read test code but must never modify them — the
checklist pins this with a typed git diff predicate. Submit via SubmitTask with result
{"verify": "repro-passes", "tests": [<repro test name>]} scoped to the repro alone, then
submit repro-passes again suite-wide so the fix is proven to break nothing else.
[CheckList]
- repro-passes passed scoped to the repro test name
- repro-passes passed suite-wide
- An inline shell predicate git diff --exit-code -- <repro test path> proved the repro test untouched
[Branch]
success: END
failure: hypothesize
[Gotcha]
- If the repro test stopped reproducing reliably after a failed fix attempt, do not keep chasing the fix: fail hypothesize so play returns to design-repro and harden the repro first.
