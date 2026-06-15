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

[Verify] tests-enumerated
fact: _Test
expect: {"min_results":1}

[SetGoal]
verify: tests-enumerated

[STEP] derive
[StepJob]
Do these in order. Do not skip a sub-step, and do not fake any of them — the only thing that
finishes this step is a real registered recipe proven by a real run.
1. The wrapper scripts `.arbiter/shim_cc.sh` and `.arbiter/shim_cxx.sh` already exist and route the
   project's real C/C++ compiler through `arbiter cc`. Reference them as the compilers in your
   configure command by their **absolute** path — a build system that probes the compiler from a
   temporary directory (cmake does) cannot resolve a relative compiler path. Do NOT recreate, edit,
   or replace them with a direct compiler path.
2. Probe the native build system (read its build files) to learn the configure command, the build
   command, the test binary it produces, and the globs of sources it compiles.
3. Write `.arbiter/recipes.yaml` (RecipeBook v2) in exactly the shape below — two-space indent, no
   tabs, lists inline [a, b, c] — wiring the two shim scripts as the compilers in the configure
   `pre` command. Strict YAML subset: NO anchors/aliases (`&`/`*`) and NO extra keys; every path
   (`compile_db.path`, `binary`, `sources`) is REPO-RELATIVE (no leading `/`). Set `binary` to the
   relative path of the test binary the build produces — it lets arbiter skip an unchanged rebuild,
   so the publish step reuses the cached build (fast) instead of recompiling. Then call
   register {"path": ".arbiter/recipes.yaml"}. register's error names the offending line/field —
   fix that one line, do not re-guess from scratch.
4. Prove the registered recipe by SUBMITTING candidate-proven — call SubmitTask with
   result `{"verify": "candidate-proven"}`. The REFEREE runs the recipe for you (it builds and
   runs the whole suite); that submission is what advances the step and publishes facts into the
   match. Do NOT try to satisfy this by calling the `run` tool yourself, or by building in Bash —
   a snapshot built outside a submitted predicate does not count, and the match will stay at
   facts.published=false no matter how many times you build. If you do call the `run` tool to
   sanity-check, its `tests` are gtest patterns (`Suite.Case`, `Suite.*`) or `["*"]` for the whole
   suite — never the test binary's filename, which matches no suite and returns `no_tests_ran`.
   The gtest harness injects its own `--gtest_output`; the recipe's `test_run` cmd is just the
   binary. A pass or a fail both prove the harness; an errored or zero-test run does not. The
   predicate is bound — only a real compile+run satisfies it.
   Your recipe MUST keep a real `src_compile` stage that compiles through the shims. A recipe with
   only a `test_run` stage runs the pre-built binary and will still pass candidate-proven, but it
   builds nothing — so the very next publish step then fails forever (facts never publish, because
   `arbiter cc` was never invoked). Do NOT drop, empty, or comment out the `src_compile` stage to
   get a green candidate-proven; the build stage is the point.

    compile_db:
      path: build/compile_commands.json
    targets:
      - id: src_compile
        binary: build/TEST_BINARY
        harness:
          kind: gtest
        src_compile:
          pre:
            - [cmake, -S, ., -B, build, -DCMAKE_C_COMPILER=ABS_REPO/.arbiter/shim_cc.sh, -DCMAKE_CXX_COMPILER=ABS_REPO/.arbiter/shim_cxx.sh, -DCMAKE_BUILD_TYPE=Debug]
          cmd: [cmake, --build, build, --target, TEST_BINARY]
        test_run:
          cmd: [./build/TEST_BINARY]
        sources: [SRC_GLOB_1, SRC_GLOB_2]

Copy that shape verbatim and substitute ONLY the CAPS placeholders: `ABS_REPO` = the repository's
absolute path, `TEST_BINARY` = the test binary's name, `SRC_GLOB_*` = the source globs the build
compiles (e.g. `src/*.cc`, `include/*.h`). Everything else — the key names, the nesting, the argv
lists — is literal. Common mistakes that make register reject the file, do NOT do these:
- There is NO `stages:`/`steps:`/`stage:` wrapper — the stage keys `src_compile`/`test_run` sit
  DIRECTLY under the target, exactly as shown.
- A target is keyed by `id:`, never `name:`. `targets:` is a sequence — each target is a `- id:`
  list item. The target's `id:` MUST be exactly `src_compile` — that is the id the
  `candidate-proven` and `gear-up-published` predicates run (`run: src_compile`). Keep `id:
  src_compile` verbatim; do NOT rename the target after the test binary or a stage, or the
  predicate cannot find it and the submit fails with `recipe_pin_mismatch`.
- Inside a stage the only keys are `pre`/`cmd`/`post`/`timeout_s`. `pre`/`post` are lists of argv
  lists; `cmd` is one argv list. There is no `configure:`/`build:`/`run:`/`command:` key.
- A stage command runs by direct exec, NOT through a shell, so NEVER write it as a shell string
  like `cd build && cmake .. && cmake --build .` — the whole string would be taken as one program
  name and fail. Each command is one argv list `[prog, arg1, arg2]`; put multiple commands as
  separate `- [..]` items under `pre`, and use cmake's `-S`/`-B` flags instead of `cd`. There is
  no `&&`, `;`, `|`, `cd`, or redirection.
- Each `pre`/`post` item is ONE complete command written as a single inline list
  `- [cmake, -S, ., -B, build, -DCMAKE_C_COMPILER=...]` — NOT one argument per line. Writing
  `pre:` and then `- cmake`, `- -S`, `- .`, `- -B` on separate lines is WRONG: that runs `cmake`
  with no arguments, then tries to run `-S` as its own program, and the configure never happens
  (so the build journals nothing → `journal_miss`). The entire cmake invocation is a SINGLE
  `- [...]` list item.
[CheckList]
- The configure command wires .arbiter/shim_cc.sh and .arbiter/shim_cxx.sh as the compilers (real compiler through arbiter cc); the shims are used as-is, not recreated
- recipe_search, then write .arbiter/recipes.yaml in the shape above, then register {"path": ".arbiter/recipes.yaml"}
- Submit candidate-proven from a real run (structured gtest output only) — never a file-exists check, marker file, or shell shortcut
[Submit] candidate-proven
[Branch]
success: publish
failure: derive

[STEP] publish
[StepJob]
Run the proven src_compile recipe so arbiter cc journals every translation unit and the engine
publishes the first facts snapshot. Only a real cc-interposed green build publishes facts; if it
does not publish, cc is not actually interposed — go back and wire it. The snapshot records each
gtest case as its generated `Suite_Case_Test` fixture type, so this build is what makes the
project's test set machine-knowable. Call scan {"scope": "*"} to pull that facts-derived test
inventory and use it as the authoritative test list for the recipe's `tests` and for what you
report — do not hand-list tests or recall the suite from memory. The match's goal is
`tests-enumerated`: after this round the referee re-runs the test index query itself and checkmates
only when the published snapshot actually contains the test set, so an enumeration you assert but
the index does not contain cannot finish the bootstrap.
[CheckList]
- Submit gear-up-published for the proven recipe
- Call scan {"scope": "*"} and treat its facts-derived test set as the authoritative test inventory
- Record any instrumentation macro key_flags recommendation for user confirmation
[Submit] gear-up-published
[Branch]
success: END
failure: derive
