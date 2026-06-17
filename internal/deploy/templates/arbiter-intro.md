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
3. **Dispatch an arbiter-executor** subagent with the Task tool, putting that `task_id` on a
   "task id:" line in the prompt along with the request. The executor does the probing, building,
   and recipe work (`register`, `import_recipes`, and `scan` are its capability-gated tools, live
   only while a `capabilities:[recipes]` opening is loaded) and finishes by calling its own
   `SubmitTask` on that `task_id` with the step's bound predicate (`{"verify":"<name>"}`).
4. Call **`mcp__arbiter__CheckStepJob`** — ask the referee to adjudicate. `complete:false` with
   `no_tasks` means you skipped step 2 — go create one; `open_tasks` means await or re-dispatch
   the listed ids; `goal_running` means call again shortly. `complete:true` advances the step (or
   ends the match); then return to step 1 for the new step.

Accept outcomes only as SubmitTask verdicts: nothing in this bootstrap is "done" because you
observed it; it is done when a typed predicate said so.

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

This bootstrap also **reconciles every wired tool surface**. It is not enough that `arbiter
init` wrote the entries — the build/index, query, recipe, and diagnostic tools must each answer
once before the repo is declared ready, so a surface that was wired but is actually broken
fails the bootstrap instead of being discovered later mid-match. Surfaces that are always
available (recipe `run`, facts `search`/`detail`, `scan`/`recipe_search`, `perf-mcp`) are
**hard-gated** by typed predicates; surfaces that depend on a host prerequisite (`gdb-mcp`'s
host `gdb`, the Clang facts toolchain) are probed and **reported** on the checklist — never
silently assumed, never used to fail a repo that is otherwise wired correctly.

## Bootstrap

1. probe the build system: identify make, cmake, or custom entry points; locate the compiler,
   gtest binary, build directory, and the repo's primary suite target.
2. Load the `recipe-derivation` opening with arbiter-curator (it ships with `arbiter init`,
   refreshed to the shipped template on every init). If the curator reports it missing, the
   deployment is broken — tell the user to re-run `arbiter init`, then stop.
3. Discover, derive, and prove recipes in `.arbiter/recipes.yaml`. Seed candidates from the
   build probe **and** the executor's `scan` tool (facts-derived test-target discovery — confirm
   it returns real candidates, not an empty stub). Each candidate must prove itself before it is
   treated as committed knowledge: call `register`, then create a referee task with
   `run: <candidate>`, representative `tests`, and
   `expect: {"overall":{"one_of":["passed","failed"]}}`. After a candidate is proven, confirm the
   book is queryable — a `recipe_search` for it must return the proven id.
4. Install `arbiter cc` interposition into every proven `src_compile` stage. Preserve the real
   compiler path and profile overlays; do not replace the build system with a synthetic command
   when a native target exists.
5. Run the instrumentation macro scan as a whole-token source scan for:
   `__SANITIZE_ADDRESS__`, `__SANITIZE_THREAD__`, and `__has_feature(*_sanitizer)`.
   Report every hit as `path:line token text`, plus a recommended `facts.key_flags` list such as
   `[-fsanitize=address]` or `[-fsanitize=thread]`. Never auto-write those flags; ask the user
   to confirm because facts relevance is a semantic choice.
6. Run the first gear-up task through the proven `src_compile` recipe with the selected profile.
   The predicate is `{"overall":"passed","facts":{"published":true}}`. If publication fails
   only because the host lacks a capable Clang (LLVM ≥ 16 / Apple ≥ 15) — not because the build
   failed — report the typed publication reason on the checklist rather than looping; builds,
   matches, and shell/mcp predicates still work without facts.
7. Reconcile the **query surface and the project's test inventory** (hard gate). A snapshot that
   published is not proven usable until it answers a query — publication is not searchability —
   and your recollection of the suite is not the project's test set: the referee's is. The
   `recipe-derivation` opening checkmates on `tests-enumerated`
   (`{"kind":"fact","query":"_Test","expect":{"min_results":1}}`), which the referee evaluates
   against the published snapshot — so the project's gtest test set (recorded in the index as the
   generated `Suite_Case_Test` fixture types) is enumerated from the index, never trusted from your
   transcript. Call `scan {"scope":"*"}` to obtain that same facts-derived set for the recipe's
   `tests` and for the report; do not hand-list tests. (Skip only when step 6 reported no snapshot
   at all — there is nothing to enumerate.)
8. Reconcile the **diagnostic companions** that `arbiter init` wired.
   - `perf-mcp` (hard gate — pure stdlib, must answer): dispatch a task whose result is
     `{"kind":"mcp","server":"perf-mcp","tool":"perf.toolchain_probe","arguments":{},`
     `"expect":[{"path":"schema_version","op":"eq","value":"perf-mcp.probe.v1"}]}`.
   - `gdb-mcp` (reported, not gated — host `gdb` may be absent or unable to launch inferiors):
     probe readiness with `gdb_diagnostics` (or note `python3 -m arbiter_engine.gdbmcp doctor
     --root .`) and record `gdb-mcp ready` / `gdb-mcp unavailable: <reason>` on the checklist.
     Do not block the bootstrap on it.
9. Confirm the base openings are present — `freeplay`, `gold-digger`, `recipe-derivation`,
   and `regression-triage` are delivered by `arbiter init` (refreshed to the shipped template on
   every init; your own-named books are never touched). Ask the curator to list them and, if any
   are missing, have the user re-run `arbiter init`.

## Checkmate — the match ending is NOT the finish line

When the match reports `finished_success` it means ONE thing: the recipe is proven and the
`tests-enumerated` goal saw the published snapshot. That is necessary but NOT sufficient. The
match almost always checkmates at the very first step — the proving build publishes facts, which
satisfies the goal immediately, so the publish step never even runs — and a freshly bootstrapped
repo is still NOT done at that moment. DO NOT stop or declare success when the match ends. You,
the player, must now reconcile every remaining wired surface YOURSELF by calling each tool
directly and seeing it answer. Asserting a surface is "ready" without calling it does NOT count:
if you did not call `mcp__perf-mcp__perf_toolchain_probe`, you have not reconciled perf-mcp.

After the match ends, run each of these and keep its result:

1. **Test inventory from the index** — call `mcp__arbiter__search` with `{"query":"_Test"}`. It
   must return the project's gtest fixture types (the real test set); take one id and call
   `mcp__arbiter__detail` with `{"fact_id":"<that id>"}`. The index is the test set; your
   transcript is not. (This is the same facts-derived inventory the executor's `scan` exposes.)
2. **perf-mcp (hard gate)** — call `mcp__perf-mcp__perf_toolchain_probe` with `{}`. It must return
   `schema_version` = `perf-mcp.probe.v1`. If it does not answer, the bootstrap is NOT complete —
   report the blocking failure, do not paper over it.
3. **gdb-mcp (reported, never gates)** — call `mcp__gdb-mcp__gdb_diagnostics` with `{}` and record
   `gdb-mcp ready` or `gdb-mcp unavailable: <reason>`. A missing or unable host `gdb` never fails
   the repo.
4. **Instrumentation macro scan** — whole-token grep the sources for `__SANITIZE_ADDRESS__`,
   `__SANITIZE_THREAD__`, and `__has_feature(*_sanitizer)`; report `path:line token` hits plus a
   suggested `facts.key_flags` (e.g. `[-fsanitize=address]`). Never auto-write flags — ask the user.
5. **Openings present** — have the curator list the books and confirm `freeplay`, `gold-digger`,
   `recipe-derivation`, and `regression-triage` are all present.

The bootstrap is complete ONLY when the proven recipe, the `search`+`detail` index answer, the
`perf-mcp` probe verdict, the `gdb-mcp` readiness line, the macro scan, and the openings list have
EACH produced a real result from a real call. If a published snapshot is blocked solely by a
missing capable Clang (LLVM ≥ 16 / Apple ≥ 15) — not a build failure — finish as "wired; facts
unavailable until Clang ≥ 16 is installed" with the typed reason recorded, rather than looping.

The final reply names the proven recipe(s), the snapshot id (or the typed reason none published),
the `search`/`detail` test-inventory result, the `perf-mcp` and `gdb-mcp` readiness lines, the
macro-scan checklist, any suggested `facts.key_flags`, and the installed openings. If a hard-gated
surface — a proven-recipe count of at least one, the `perf-mcp` probe, or the facts query — cannot
produce its verdict, report the blocking predicate instead of declaring the bootstrap complete.
