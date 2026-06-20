---
name: arbiter-intro
description: Bootstrap Arbiter in this repository through an adjudicated match.
---

Run an adjudicated bootstrap match. Do not silently edit committed config based on judgment;
every durable change is proven or reported as a checklist item.

You operate as the player seat throughout — you analyze and dispatch, you never execute. Your
job is to DRIVE a refereed loop, not to wait for work to appear. The match begins with an empty
task ledger and stays empty until you author tasks into it: there are no pre-existing tasks to
"find" or "execute", and an empty `ShowStepJob` / `ListTask` ledger is the normal start of every
step — never an error, never a sign you are finished, never a reason to stop and re-read these
instructions. YOU, the player, create every task; the referee then adjudicates it.

**Your player-seat tools are MCP tools on the `arbiter` server that load on demand — so before
you enter the loop, your FIRST action is to load all their schemas with ONE `ToolSearch` call,
using exactly this query (copy it verbatim):**

    select:mcp__arbiter__ShowStepJob,mcp__arbiter__CreateTask,mcp__arbiter__CheckStepJob,mcp__arbiter__ListTask,mcp__arbiter__ReviewTask,mcp__arbiter__SubmitCheckpoint,mcp__arbiter__search,mcp__arbiter__detail,mcp__perf-mcp__perf_toolchain_probe,mcp__gdb-mcp__gdb_diagnostics

After that single `ToolSearch`, every one of those tools is directly callable by its full name
(`mcp__arbiter__ShowStepJob`, `mcp__arbiter__CreateTask`, `mcp__arbiter__CheckStepJob`, …). From
then on, just CALL them — do not `ToolSearch` for them again. And do NOT invoke the `/arbiter-play`
skill to "drive the loop" for you: there is no driver skill — YOU drive the loop by calling these
tools yourself, right here. If a player tool ever seems missing, you have simply not run that one
`ToolSearch` load yet — run it, then call the tool; never conclude the tool does not exist, never
stall re-reading this skill, and never `Read` a directory or state file to "check for tasks". (The
curator and executor are subagents you reach with the Task tool, not skills.)

First dispatch the arbiter-curator subagent (Task tool) to `LoadPlayBook` the opening. Then, for
each step the opening presents, run THIS EXACT LOOP in order, repeating it step after step until
the referee ends the match:

1. Call **`mcp__arbiter__ShowStepJob`** — read the current step's job text, checklist, and the
   name of its bound `submit` predicate (the `[Verify]` name you must ultimately submit). This is
   your ONLY view of the flow; future steps are hidden, so never try to plan past the current step.
2. Call **`mcp__arbiter__CreateTask`** — author ONE self-contained executor work item for this
   step. Its `request` string must carry the goal, the scope limits, and the exact bound predicate
   name from step 1 for the executor to submit. This is the player's job and yours alone — nobody
   hands you a task; you write it. CreateTask returns a `task_id`.
3. **Dispatch the right executor subagent** with the Task tool, putting that `task_id` on a
   "task id:" line in the prompt along with the request. Route by step: the recipe/facts steps
   (`derive`, `prove`, `enumerate`, `cover`) go to **arbiter-executor** (`register`, `import_recipes`,
   `scan` are its capability-gated tools, live only while a `capabilities:[recipes]` opening is
   loaded); the diagnostic reconcile steps (`reconcile-perf`, `reconcile-diag`) go to
   **arbiter-debugger**, the only subagent wired with the `perf-mcp` and `gdb-mcp` companion tools
   needed to drive a real perf scan and a real gdb session. Whichever you dispatch finishes by
   calling its own `SubmitTask` on that `task_id` with the step's bound predicate
   (`{"verify":"<name>"}`).
4. Call **`mcp__arbiter__CheckStepJob`** — ask the referee to adjudicate. `complete:false` with
   `no_tasks` means you skipped step 2 — go create one; `open_tasks` means await or re-dispatch
   the listed ids; `goal_running` means call again shortly. `complete:true` advances the step (or
   ends the match); then return to step 1 for the new step.

**Checkpoint steps are the exception to steps 2–3.** When `ShowStepJob` marks the current step as a
checkpoint (it carries a `checkpoint` question instead of a checklist — the opening's `confirm`
step), do NOT `CreateTask` or dispatch anyone: put that exact question to the user with
`AskUserQuestion` (pass / fail), then call **`mcp__arbiter__SubmitCheckpoint`** with
`{"decision":"pass"|"fail"}` relaying their real choice (pass → advance, fail → loop the step). In
an interactive session relay the real human choice and never decide for them — the checkpoint
exists to get a real human yes. ONLY when this is a non-interactive / headless run where
`AskUserQuestion` cannot reach a person (it errors or returns no usable answer) do you avoid
looping forever: every step in this opening was already referee-verified, so `SubmitCheckpoint`
`{"decision":"pass"}` once and note in your final report that the confirmation was auto-approved
because the run is non-interactive (no human present). Never auto-pass a checkpoint a human could
have answered.

Accept outcomes only as SubmitTask/SubmitCheckpoint verdicts: nothing in this bootstrap is "done"
because you observed it; it is done when a typed predicate or the user said so.

**Non-negotiable rules — follow exactly, do not improvise:**
- Load EXACTLY the `recipe-derivation` opening, by name: dispatch the curator to
  `LoadPlayBook` with `name` = `recipe-derivation`. Do NOT let the curator auto-select, do NOT
  load `freeplay` or any other opening, and do NOT switch openings when a step is hard — work
  the step you are on until its bound predicate passes.
- NEVER invent your own steps, "smoke tests", or marker/placeholder files and call them done.
  The only work that counts is what the loaded opening's steps dispatch through the referee.
- Finish every task by submitting the step's bound predicate exactly as `ShowStepJob` names it
  (`{"verify":"<name>"}`). Never substitute a shell file-exists check or a predicate of your own.
  A bound predicate that needs a real compile/run is satisfied only by a real compile/run — never
  by writing a file.
- Learn match state ONLY through `ShowStepJob` / `ListTask` / `ReviewTask` / `CheckStepJob`. Never
  `Read` a directory or a state file directly.
- On a tool error, read the message and fix the cause; do not resubmit the same thing hoping it
  passes, and never treat a tool failure as a step completing.

This bootstrap **proves every wired tool surface on its REAL function** — not a version/readiness
probe — and it does so through the loaded opening's own steps, each gated by a referee-verified
predicate, so a surface that was wired but is actually broken fails its step instead of being
discovered later. The `recipe-derivation` opening walks these steps; drive each with the loop above
and the opening's `ShowStepJob` text tells you exactly what to submit:

- **derive** → `build-published`: author a recipe and prove its `arbiter cc`-interposed build
  PUBLISHES the first facts snapshot — the build half only, run under a no-match filter so it needs
  no test environment yet (so there is no separate "publish" step). (arbiter-executor.)
- **prove** → `candidate-proven`: discover the runtime environment the test needs (CI config first),
  add it to the recipe, and prove the whole suite actually RUNS through the recipe — this is where an
  environment-shaped failure means a recipe defect to fix, not a test result. (arbiter-executor.)
- **enumerate** → `tests-enumerated`: the referee re-queries the published `_Test` index itself,
  proving facts published WITH the project's test set (recorded as the generated `Suite_Case_Test`
  fixture types) — enumerated from the index, never trusted from your transcript. An empty index
  means the derive build was not cc-interposed (or the recipe had no real `src_compile` stage) —
  fix that, do not loop. Call `scan {"scope":"*"}` for the authoritative test set to report. If
  facts publication is blocked only by a missing capable Clang (LLVM ≥ 16 / Apple ≥ 15) — not a
  build failure — report the typed reason; builds, matches, and shell/mcp predicates work without
  facts. This step also REGISTERS the whole suite: a recipe for every test binary the build produces
  (`import_recipes`, one target per binary) so a clean checkout can `run` any suite from the
  committed book — registered here, built+indexed at the next step.
- **cover** → `suite-covered`: build and INDEX the whole project test suite through `arbiter cc` (one
  or a few parallel cc-interposed builds of the test tree; the facts index merges incrementally), so
  the index carries the project's tests, not just the one derive binary. The referee measures
  built/declared coverage over the project scope (vendored third-party excluded) and passes only at
  substantial coverage — proving one binary scores ~0. This is the bootstrap's FULL-COVERAGE purpose;
  it does not require tests to pass, only to build+index, and skips host-unbuildable binaries.
  (arbiter-executor.)
- **reconcile-perf** → `perf-static-scan`: the referee runs `perf.scan_c` over the project (a REAL
  static analysis, not `perf.toolchain_probe`). In the same step also call `perf.measure_command`
  and `perf.explain_finding` and report their typed results. (arbiter-debugger.)
- **reconcile-diag** → `gdb-debugs-real-binary`: the referee compiles a tiny program and drives gdb
  through break→run→inspect — proving real debugging where the host can, reporting-and-passing
  where gdb is absent or cannot launch inferiors (gdb is reported, never used to fail the repo). In
  the same step drive a real gdb-mcp session against the proven binary
  (`gdb_start`→`gdb_breakpoint`→`gdb_exec`→`gdb_stack`/`gdb_eval`→`gdb_stop`), exercise
  `import_recipes` (then `recipe_search` to confirm the import is queryable), and append a
  `NotePlaybook` gotcha. (arbiter-debugger.)
- **confirm** → a `[Checkpoint]`: put its question to the user with `AskUserQuestion` and relay the
  answer via `SubmitCheckpoint`. This proves the human-confirmation surface.

Three things the opening leaves to you — do them and fold them into the final report:
- **Probe the build system** before loading the opening (make/cmake entry points, compiler, gtest
  binary, build dir, and which test target to prove) so your derive task is concrete. When the
  project exposes MANY test executables, do NOT pick an aggregate / "merged" / "all-tests" target
  and do NOT build the whole project — those compile a large fraction of the codebase, so the
  proving build is slow and frequently breaks on an unrelated translation unit. Choose the SMALLEST
  self-contained test executable instead — ideally one built from a single test source file with the
  fewest link dependencies. One small gtest binary is enough to prove the recipe and publish the
  facts index; the bootstrap does not need the project's whole test suite built.
- **Instrumentation macro scan**: whole-token grep the sources for `__SANITIZE_ADDRESS__`,
  `__SANITIZE_THREAD__`, and `__has_feature(*_sanitizer)`; report `path:line token` hits and a
  suggested `facts.key_flags` (e.g. `[-fsanitize=address]`). Never auto-write flags — ask the user,
  because facts relevance is a semantic choice.
- **Openings present**: have the curator list the books and confirm `freeplay`, `gold-digger`,
  `recipe-derivation`, and `regression-triage` are all there (re-run `arbiter init` if any are
  missing).

## Checkmate

The opening runs every step to `END` — it does NOT checkmate early. The bootstrap is complete only
when the match reaches `finished_success` (the `confirm` checkpoint passed) AND each gated step
produced its verdict — every surface above was proven on its real function, not asserted. A step
whose predicate cannot pass keeps the match open; report the blocking predicate rather than
declaring success. Report a surface as proven only because its step's predicate passed (or its tool
returned a real result), never because you observed it.

The final reply names: the proven recipe id, the snapshot id (or the typed reason none published),
the `scan`/`search` test inventory, the recipe-book coverage (how many test binaries got a
registered recipe vs the single one proven), the perf-mcp scan/measure/explain results, the gdb debug result
and the gdb-mcp session outputs, the macro-scan checklist with any suggested `facts.key_flags`, and
the installed openings.
