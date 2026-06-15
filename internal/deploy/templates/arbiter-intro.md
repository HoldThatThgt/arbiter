---
name: arbiter-intro
description: Bootstrap Arbiter in this repository through an adjudicated match.
---

Run an adjudicated bootstrap match. Do not silently edit committed config based on judgment;
every durable change is proven or reported as a checklist item.

You operate as the player seat throughout — you analyze and dispatch, you never execute.
Load openings via the arbiter-curator subagent (Task tool); then, for each step the opening
presents, run the same refereed loop the play skill uses:
**ShowStepJob** (read the step's job, checklist, and any bound `submit` predicate) →
**CreateTask** (define one self-contained unit of work) → dispatch an **arbiter-executor**
subagent with the Task tool (it does the probing and recipe work — `register`,
`import_recipes`, and `scan` are its capability-gated tools, live only while a
`capabilities:[recipes]` opening is loaded) → **CheckStepJob** (the referee adjudicates and
advances the round). Accept outcomes only as SubmitTask verdicts: nothing in this bootstrap is
"done" because you observed it; it is done when a typed predicate said so.

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
   (`{"kind":"fact","query":"TestBody","expect":{"complete":true,"min_results":1}}`), which the
   referee evaluates against the published snapshot — so the complete gtest TestBody set is
   enumerated from the index, never trusted from your transcript. Call `scan {"scope":"*"}` to
   obtain that same facts-derived set for the recipe's `tests` and for the report; do not
   hand-list tests. (Skip only when step 6 reported no snapshot at all — there is nothing to
   enumerate.)
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

## Checkmate

A repo is bootstrapped only when every always-available surface has produced a typed verdict and
every host-dependent surface has been probed and reported.

- **Hard gates** (each must produce its verdict, else keep the match open or report the blocking
  predicate): a proven-recipe count of at least one (each via `run` ⇒ `overall ∈
  {passed,failed}`); the `perf-mcp` toolchain-probe verdict; and — whenever a snapshot
  published — a successful facts query against it.
- **Expected, host-gated:** a published snapshot. If publication is blocked solely by a missing
  capable Clang, finish as "wired; facts unavailable until Clang ≥ 16 is installed" with the
  typed reason recorded, rather than looping.
- **Reported readiness (never gates):** `gdb-mcp` ready/unavailable, the Clang facts capability,
  the macro-scan checklist, and any suggested `facts.key_flags`.

The final reply names the proven recipes, the snapshot id (or the typed reason none published),
the facts-query result, the `perf-mcp` and `gdb-mcp` readiness lines, the macro-scan checklist,
any suggested `facts.key_flags`, and the installed openings. If a hard-gated surface cannot
produce its verdict, report the blocking predicate instead of declaring bootstrap complete.
