---
name: recipe-derivation
description: Capability-gated opening for deriving and proving committed run recipes.
capabilities: [recipes]
max_steps: 48
verify_policy: named
---

[Verify] candidate-proven
run: src_compile
tests: ["*"]
expect: {"overall":{"one_of":["passed","failed"]}}

[Verify] gear-up-published
run: src_compile
tests: ["src_compile"]
expect: {"overall":"passed","facts":{"published":true}}

[SetGoal]
verify: gear-up-published

[STEP] derive
[StepJob]
Probe the native build system and draft the smallest portable src_compile recipe — a gtest
harness, configure+build commands, and sources globs — with arbiter cc interposed into the
compile stage (preserve the real compiler and profile overlays, never a synthetic command).
Register it, then prove it actually builds and emits structured gtest output: a pass or a fail
both prove the harness works, an errored build does not. The predicate is bound; you cannot
substitute a file-exists check for a real run.
[CheckList]
- recipe_search before adding a target, then register the src_compile recipe book
- The src_compile compile stage invokes the real compiler through arbiter cc
- Submit candidate-proven from a real run (structured gtest output only)
[Submit] candidate-proven
[Branch]
success: publish
failure: derive

[STEP] publish
[StepJob]
Run the proven src_compile recipe so arbiter cc journals every translation unit and the engine
publishes the first facts snapshot. Only a real cc-interposed green build publishes facts; if it
does not publish, cc is not actually interposed — go back and wire it.
[CheckList]
- Submit gear-up-published for the proven recipe
- Record any instrumentation macro key_flags recommendation for user confirmation
[Submit] gear-up-published
[Branch]
success: END
failure: derive
