---
name: arbiter-intro
description: Bootstrap Arbiter in this repository through an adjudicated match.
---

Run an adjudicated bootstrap match. Do not silently edit committed config based on judgment;
every durable change is proven or reported as a checklist item.

## Bootstrap

1. probe the build system: identify make, cmake, or custom entry points; locate the compiler,
   gtest binary, build directory, and the repo's primary suite target.
2. Load the `recipe-derivation` opening with arbiter-curator. If it is not installed yet, use
   freeplay only to derive the first recipe, then install the base openings.
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
7. Ensure the base openings exist: `freeplay`, `gold-digger`, and `recipe-derivation`.

## Checkmate

Finish only when the evidence has both a proven-recipe count and a published snapshot. The final
reply names the proven recipes, the snapshot id, the macro-scan checklist, any suggested
`facts.key_flags`, and the installed openings. If any step cannot be proven, keep the match open
or report the blocking predicate instead of declaring bootstrap complete.
