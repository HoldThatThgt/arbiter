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
Probe the native build system, then write `.arbiter/recipes.yaml` (RecipeBook v2) and register it
with register {"path": ".arbiter/recipes.yaml"}. Fill exactly this shape (two-space indent, no
tabs; lists are inline [a, b, c]); register's error names the offending line/field if the YAML is
off — fix that line, do not re-guess from scratch:

    compile_db:
      path: build/compile_commands.json
    targets:
      - id: src_compile
        harness:
          kind: gtest
        src_compile:
          pre:
            - [<configure: e.g. cmake -S . -B build -DCMAKE_C_COMPILER=.arbiter/shim_cc.sh -DCMAKE_CXX_COMPILER=.arbiter/shim_cxx.sh>]
          cmd: [<build: e.g. cmake --build build --target your_test_binary>]
        test_run:
          cmd: [<run: e.g. ./build/your_test_binary>]
        sources: [<globs of the compiled sources, e.g. src/*.cc, src/*.cpp>]

The compile stage must invoke the real compiler through arbiter cc (the shim_cc.sh/shim_cxx.sh
wrappers above each `exec arbiter cc -- <real-cc> "$@"`), never a synthetic command. Once
registered, prove it actually builds and emits structured gtest output — a pass or a fail both
prove the harness works, an errored build does not. The predicate is bound; you cannot substitute
a file-exists check for a real run.
[CheckList]
- recipe_search before adding a target; write .arbiter/recipes.yaml in the shape above, then register {"path": ".arbiter/recipes.yaml"}
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
