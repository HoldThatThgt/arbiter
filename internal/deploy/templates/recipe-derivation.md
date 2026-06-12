---
name: recipe-derivation
description: Capability-gated opening for deriving and proving committed run recipes.
capabilities: [recipes]
max_steps: 48
---

[Verify] gear-up-published
run: src_compile
tests: ["src_compile"]
expect: {"overall":"passed","facts":{"published":true}}

[Verify] candidate-proven
run: candidate
tests: ["*"]
expect: {"overall":{"one_of":["passed","failed"]}}

[SetGoal]
verify: gear-up-published

[STEP] gear-up
[StepJob]
Inspect the current recipe book and publish facts if a src_compile recipe already exists.
[CheckList]
- Submit gear-up-published when a src_compile recipe is already proven
- If no src_compile recipe exists yet, record that derivation starts from build probes
[Branch]
success: derive
failure: derive

[STEP] derive
[StepJob]
Probe the native build system and draft the smallest portable recipe that compiles, launches, and
emits structured gtest output.
[CheckList]
- Use recipe_search before adding a new target
- Draft committed YAML with src_compile, test_run, harness.kind=gtest, and sources globs
[Branch]
success: prove
failure: derive

[STEP] prove
[StepJob]
Register the candidate recipe and prove it through the referee before treating it as knowledge.
[CheckList]
- Call register for the candidate recipe book
- Submit candidate-proven using structured gtest output only
[Branch]
success: install
failure: derive

[STEP] install
[StepJob]
Install arbiter cc interposition into proven src_compile stages and preserve native compiler
commands.
[CheckList]
- src_compile invokes the real compiler through arbiter cc
- Profiles are represented as overlays instead of separate duplicate recipes
[Branch]
success: publish
failure: prove

[STEP] publish
[StepJob]
Run the proven src_compile recipe once to publish the first facts snapshot for the derived target.
[CheckList]
- Submit gear-up-published for the proven recipe
- Record any instrumentation macro key_flags recommendation for user confirmation
[Branch]
success: END
failure: install
