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

## Bootstrap

1. probe the build system: identify make, cmake, or custom entry points; locate the compiler,
   gtest binary, build directory, and the repo's primary suite target.
2. Load the `recipe-derivation` opening with arbiter-curator (it ships with `arbiter init`,
   write-if-missing). If the curator reports it missing, the deployment is stale — tell the
   user to re-run `arbiter init`, then stop.
3. Derive candidate recipes in `.arbiter/recipes.yaml`. Each candidate must prove itself before
   it is treated as committed knowledge: call `register`, then create a referee task with
   `run: <candidate>`, representative `tests`, and
   `expect: {"overall":{"one_of":["passed","failed"]}}`.
4. Install `arbiter cc` interposition into every proven `src_compile` stage. Preserve the real
   compiler path and profile overlays; do not replace the build system with a synthetic command
   when a native target exists.
5. Run the instrumentation macro scan as a whole-token source scan for:
   `__SANITIZE_ADDRESS__`, `__SANITIZE_THREAD__`, and `__has_feature(*_sanitizer)`.
   Report every hit as `path:line token text`, plus a recommended `facts.key_flags` list such as
   `[-fsanitize=address]` or `[-fsanitize=thread]`. Never auto-write those flags; ask the user
   to confirm because facts relevance is a semantic choice.
6. Run the first gear-up task through the proven `src_compile` recipe with the selected profile.
   The predicate is `{"overall":"passed","facts":{"published":true}}`.
7. Confirm the base openings are present — `freeplay`, `gold-digger`, `recipe-derivation`,
   and `regression-triage` are delivered by `arbiter init` (write-if-missing); ask the curator
   to list them and, if any are missing, have the user re-run `arbiter init`.

## Checkmate

Finish only when the evidence has both a proven-recipe count and a published snapshot. The final
reply names the proven recipes, the snapshot id, the macro-scan checklist, any suggested
`facts.key_flags`, and the installed openings. If any step cannot be proven, keep the match open
or report the blocking predicate instead of declaring bootstrap complete.
