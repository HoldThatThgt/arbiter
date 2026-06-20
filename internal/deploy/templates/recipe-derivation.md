---
name: recipe-derivation
description: Capability-gated opening for deriving and proving committed run recipes, then proving every wired tool surface on its real function.
capabilities: [recipes]
max_steps: 64
verify_policy: named
---

[Verify] candidate-proven
run: src_compile
tests: ["*"]
expect: {"overall":{"one_of":["passed","failed"]}}

# The derive (author) gate: the cc-interposed build PUBLISHED the facts index. It runs src_compile
# under a filter that matches no test (so no test environment is needed yet — that is the prove
# step), and asserts ONLY facts.published: the build compiled the test sources and the index
# published. Deliberately NO "overall" clause — a no-match run is overall=errored, and the build,
# not the test, is what this step proves; the prove step proves the suite runs. facts.published
# cannot be faked (the index is built by libclang from the journaled translation units), so this is
# a real build proof, not a green-by-zero-tests shortcut.
[Verify] build-published
run: src_compile
tests: ["src_compile"]
expect: {"facts":{"published":true}}

[Verify] tests-enumerated
fact: _Test
expect: {"min_results":1}

# Full-suite coverage gate: how many of the project's DECLARED gtest cases the facts index
# actually carries (built / declared over the project scope, vendored third-party excluded).
# `declared` is the build-independent AST scan of source; `built` is produced ONLY by a real
# arbiter cc-interposed build, so the ratio cannot be faked — proving one small binary scores
# ~0, building+indexing the whole suite drives it up. The referee re-runs discovery.coverage()
# against the live snapshot; pass requires substantial coverage (tune the 0.50 floor per repo).
[Verify] suite-covered
shell: PYTHONPATH=.arbiter/engine python3 -c 'import sys; from arbiter_engine.runs import discovery as d; c=d.coverage("."); sys.stderr.write("suite-covered "+repr(c)+chr(10)); sys.exit(0 if c["ratio"]>=0.50 else 1)'
timeout_s: 900

# perf-mcp proven on its REAL function, not a version probe: the referee itself calls
# perf.scan_c over the project (root defaults to the repo) and requires a schema-versioned
# findings payload. `findings` may be empty on a repo with no C/C++ hot paths — that the
# scan RAN and returned the typed shape is the proof; do not require a non-empty list.
[Verify] perf-static-scan
mcp: perf-mcp perf.scan_c
arguments: {}
expect: [{"path":"schema_version","op":"eq","value":"perf-mcp.scan.v1"},{"path":"findings","op":"exists"}]

# perf-mcp's measurement path proven on a trivial, repo-agnostic command.
[Verify] perf-command-measured
mcp: perf-mcp perf.measure_command
arguments: {"command":["true"],"repeat":2}
expect: [{"path":"schema_version","op":"eq","value":"perf-mcp.measure.v1"}]

# gdb-mcp proven on REAL debugging, not just gdb_diagnostics: compile a tiny program with
# whatever C compiler is present and drive gdb through break->run->next->inspect, asserting it
# read the live value (x=41). Per the intro design gdb is REPORTED, not gated: a host that lacks
# gdb or a compiler, or forbids launching inferiors (unsigned gdb / DWARF5 mismatch on macOS),
# must not fail the repo, so this always exits 0 — it PROVES real debugging on boxes where gdb
# can (the report says "PROVED"), and reports the host limitation otherwise. The shell is the
# referee's, re-run independently.
[Verify] gdb-debugs-real-binary
shell: T=$(mktemp -d); printf 'int main(){int x=41; volatile int y=x+1; return y-42;}\n' > "$T/g.c"; CC=$(command -v cc || command -v gcc || command -v gcc-12 || command -v gcc-10 || command -v clang || command -v clang-16); if [ -z "$CC" ]; then echo "gdb gate: no C compiler on host — reported, not gated"; exit 0; fi; "$CC" -g -O0 "$T/g.c" -o "$T/g" 2>/dev/null || { echo "gdb gate: trivial compile failed — reported, not gated"; exit 0; }; if ! command -v gdb >/dev/null 2>&1; then echo "gdb gate: gdb absent — reported, not gated"; exit 0; fi; O=$(gdb -nx -batch -ex 'break main' -ex run -ex next -ex 'print x' -ex quit "$T/g" 2>&1); if echo "$O" | grep -q '= 41'; then echo "gdb gate: PROVED real debugging — break main, run, read live x=41"; else echo "gdb gate: gdb present but could not debug on this host (host limitation) — reported, not gated: $(echo "$O" | tr -d '\n' | tail -c 160)"; fi; exit 0
timeout_s: 180

[STEP] derive
[StepJob]
Author a recipe whose cc-interposed build PUBLISHES the facts index. Do these in order; do not skip
or fake a sub-step — the only thing that finishes this step is a real cc-interposed build that
publishes facts (the prove step that follows runs the suite). Probe and wire first, write the build
half of the recipe, then submit the build proof.
1. Probe the native build system (read its build files) to learn the configure command, the build
   command, the test binary it produces, the globs of sources it compiles, AND the project's real
   C and C++ compilers (the ones the build would use — e.g. what `cc`/`c++` resolve to, or what the
   build files name). In the SAME pass, find the project's OWN build/test ENTRY POINT — a wrapper the
   project already drives its build with: a top-level build or test script (a `build.sh` /
   `run_tests.sh` / `make`-target of that kind), or the exact configure+build commands the CI config
   runs. If one exists it already encodes the right flags, build order, and setup the project expects,
   so you will REUSE it in sub-step 2 instead of reconstructing an equivalent by hand — reconstructing
   a build the project already wraps is how recipes turn into long, fragile, hand-built command
   strings that break on details the wrapper handles for you. When the build defines MANY test
   executables, pick the SMALLEST self-contained one to prove — ideally a target built from a single
   test source file with the fewest link dependencies — NOT an aggregate / "merged" / "all-tests"
   target and NOT a "build everything" target. One small gtest binary proves the recipe and publishes
   the facts index; a merged or whole-project target compiles a huge fraction of the codebase, so its
   build is slow and often fails on an unrelated translation unit, which blocks this step for reasons
   that have nothing to do with your recipe. Pick a target whose tests actually RUN on THIS build
   host, not merely compile: a test source may guard all of its cases behind a platform/architecture
   `#if` (an x86-only or Windows-only test), so on a different host it compiles to a binary with ZERO
   tests — that target builds and publishes facts here, but can never be proven in the prove step.
   Favor a small target whose test cases are unconditional / portable.
2. Wire that build through `arbiter cc` so every translation unit is journaled — that journal is
   what the facts index is built from. `arbiter cc` is a compiler LAUNCHER: given
   `arbiter cc --root ABS_REPO -- <compiler> <args>` it records the compile, then execs
   `<compiler> <args>` UNCHANGED. So do NOT replace or rewrite the project's compiler or toolchain —
   prepend `arbiter cc` in front of whatever compiler the build already uses (the way ccache is
   wired).
   If sub-step 1 found the project's own build ENTRY, REUSE IT: make that script / target / CI
   command the stage's `cmd` and interpose `arbiter cc` through the hook it already honors, rather
   than hand-rewriting the configure+build it performs. A wrapper that honors `CC`/`CXX` (most
   `make`/autotools wrappers, many `build.sh`) — set them to the `arbiter cc` prefix where the
   wrapper reads them and let it run unchanged; a wrapper that forwards extra flags to its configure
   — pass the compiler-launcher flags through it. The interposition mechanics are identical to the
   forms below; you are just letting the project's entry run the build instead of retyping it. Spell
   out the configure+build commands yourself ONLY when the project has no such entry. Either way, use
   the LEAST invasive interposition form, chosen by build system:
   - **A CMakeLists.txt drives the build → use the compiler-launcher flags.** Add to the configure
     command (alongside whatever else it needs):
       `-DCMAKE_C_COMPILER_LAUNCHER=arbiter;cc;--root;ABS_REPO;--`
       `-DCMAKE_CXX_COMPILER_LAUNCHER=arbiter;cc;--root;ABS_REPO;--`
     CMake splits each `;`-list into the argv `arbiter cc --root ABS_REPO --` and prepends it before
     the compiler it detected — so it keeps using the project's REAL compilers (its own compiler-id
     detection runs WITHOUT this, so there is no probe issue) and just routes each compile through
     `arbiter cc`. Keep the trailing `--`. The launcher only fires when the build actually compiles,
     so the stage's `cmd` (e.g. `cmake --build`) must still run. In `.arbiter/recipes.yaml`, write
     each whole `-D…LAUNCHER=…` flag as ONE double-quoted list item —
     `"-DCMAKE_C_COMPILER_LAUNCHER=arbiter;cc;--root;ABS_REPO;--"` — never split it on the
     semicolons. (Works identically with the Ninja generator.)
   - **make / autotools / a `configure` script that honors `CC`/`CXX` → prefix those.** Pass them ON
     the build command (in the stage's `cmd` argv, as double-quoted tokens) so they override the
     environment: `"CC=arbiter cc --root ABS_REPO -- REAL_CC"` and
     `"CXX=arbiter cc --root ABS_REPO -- REAL_CXX"`. This prepends `arbiter cc` without replacing the
     toolchain.
   - **The build offers NEITHER a launcher hook NOR a `CC`/`CXX` override → standalone shim scripts,
     LAST RESORT.** If `.arbiter/shim_cc.sh` / `.arbiter/shim_cxx.sh` already exist, use them as-is
     (an operator may have pre-wired an unusual toolchain). Otherwise write each as a one-line shell
     script ON DISK (these are scripts, not recipe argv) and `chmod +x` both:
       `.arbiter/shim_cc.sh`  contains:  `#!/bin/sh` (newline) `exec arbiter cc --root ABS_REPO -- REAL_CC "$@"`
       `.arbiter/shim_cxx.sh` contains:  `#!/bin/sh` (newline) `exec arbiter cc --root ABS_REPO -- REAL_CXX "$@"`
     then wire the shims as the compilers by their **absolute** path (a build that probes the
     compiler from a temporary directory — cmake does — cannot resolve a relative path).
   In every form: `arbiter` is on PATH, so the bare token works (if the build's environment cannot
   resolve it, use arbiter's absolute path). `--root ABS_REPO` (the repository's absolute path) is
   REQUIRED so journals land in the repo's `.arbiter`, not a build subdir's. REAL_CC / REAL_CXX is
   the compiler the build uses by default — what `cc`/`c++` resolve to, or the build's own
   `$CC`/`$CXX`, as found in step 1 — wrap THAT even if it is not Clang: arbiter re-parses the
   journaled translation units into the index with its OWN capable Clang internally, so the build
   compiler need not be Clang for facts to publish (the "capable Clang" requirement is on arbiter's
   index, never on your build). Do NOT switch the build to a different compiler than it normally uses
   just because another one is installed.
3. Write `.arbiter/recipes.yaml` (RecipeBook v2) in exactly the shape below — two-space indent, no
   tabs, lists inline [a, b, c] — wiring the build through `arbiter cc` in the configure `pre`
   command via the form you chose in step 2. At THIS step fill only the BUILD half: the top-level
   `compile_db:` section and a real `src_compile` stage routed through `arbiter cc`, plus a BARE
   `test_run.cmd` (just the binary). Leave the runtime-environment slots — `env:`, `workdir:`, and
   `test_run.pre` — for the prove step, where you discover what the suite needs to actually RUN.
   Strict YAML subset: NO anchors/aliases (`&`/`*`) and NO extra keys; every path
   (`compile_db.path`, `binary`, `sources`) is REPO-RELATIVE (no leading `/`). Set `binary` to the
   FULL repo-relative path the build actually writes the test binary to — including its build
   directory, e.g. `build/<name>`, NOT just `<name>`. It lets arbiter skip an unchanged rebuild,
   so a later run reuses the cached build (fast) instead of recompiling; a `binary` that does
   not point at the real output file disables that cache and makes the publish snapshot incomplete.
   Then call register {"path": ".arbiter/recipes.yaml"}. register's error names the offending
   line/field — fix that one line, do not re-guess from scratch.
4. Prove the BUILD by SUBMITTING build-published — call SubmitTask with
   result `{"verify": "build-published"}`. The REFEREE runs the recipe for you: it BUILDS through
   `arbiter cc` and confirms the facts index published. It runs the binary under a filter that
   matches no test (`tests: ["src_compile"]`), so this proves the BUILD and the index WITHOUT needing
   the test's runtime environment yet — that is the prove step's job, next. (The run is overall
   `errored` because no test matched; that is expected — this gate asserts only that facts published,
   which the cc-interposed build does on its own.) Do NOT try to satisfy this by calling the `run`
   tool yourself, or by building in Bash — a snapshot built outside a submitted predicate does not
   count, and the match stays at facts.published=false no matter how many times you build. Your
   recipe MUST keep a real `src_compile` stage that compiles through `arbiter cc` (via the
   launcher / prefix / shim form from step 2). A recipe with only a `test_run` stage builds nothing —
   so the `_Test` index stays empty and every later step fails forever (facts never publish, because
   `arbiter cc` was never invoked). The cc-interposed build here is what publishes the first facts
   snapshot; do NOT drop, empty, or comment out the `src_compile` stage to get a green — the build
   stage is the point.

    compile_db:
      path: build/compile_commands.json
    targets:
      - id: src_compile
        binary: build/TEST_BINARY
        harness:
          kind: gtest
        workdir: .                          # dir the test runs from (filled at the prove step); OMIT to default to the repo root
        env:                                # env vars the test needs to run (filled at the prove step); OMIT the whole key if none
          SOME_VAR: some-value
        src_compile:
          pre:
            - [cmake, -S, ., -B, build, "-DCMAKE_C_COMPILER_LAUNCHER=arbiter;cc;--root;ABS_REPO;--", "-DCMAKE_CXX_COMPILER_LAUNCHER=arbiter;cc;--root;ABS_REPO;--", -DCMAKE_BUILD_TYPE=Debug]
          cmd: [cmake, --build, build, --target, TEST_BINARY]
        test_run:
          pre:                              # runtime setup (filled at the prove step: start a service, generate config/data); OMIT if none
            - [sh, -c, "./scripts/setup-test-env.sh"]
          cmd: [./build/TEST_BINARY]        # the BARE binary only — setup goes in env/pre/workdir, never here
        sources: [SRC_GLOB_1, SRC_GLOB_2]

Copy that shape verbatim and substitute ONLY the CAPS placeholders: `ABS_REPO` = the repository's
absolute path, `TEST_BINARY` = the test binary's name, `SRC_GLOB_*` = the source globs the build
compiles (e.g. `src/*.cc`, `include/*.h`). Everything else — the key names, the nesting, the argv
lists — is literal — EXCEPT the runtime-environment slots `workdir:`, `env:`, and `test_run.pre`,
which you fill at the PROVE step (the `SOME_VAR` / `setup-test-env.sh` shown are illustrative —
replace or remove them): at derive, leave them blank or drop each line, since the build proof
(build-published) runs the binary under a no-match filter and needs no test environment.
The shape shows the CMake compiler-launcher form (sub-step 2, first bullet); for
a make/autotools build, drop the `-D…LAUNCHER` flags and instead carry the `CC`/`CXX` prefix tokens
on the build command, e.g. `cmd: [make, "CC=arbiter cc --root ABS_REPO -- REAL_CC", "CXX=arbiter cc
--root ABS_REPO -- REAL_CXX", -C, build]`. Common mistakes that make register reject the file, do
NOT do these:
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
  `- [cmake, -S, ., -B, build, "-DCMAKE_C_COMPILER_LAUNCHER=arbiter;cc;--root;ABS_REPO;--"]` — NOT one argument per line. Writing
  `pre:` and then `- cmake`, `- -S`, `- .`, `- -B` on separate lines is WRONG: that runs `cmake`
  with no arguments, then tries to run `-S` as its own program, and the configure never happens
  (so the build journals nothing → `journal_miss`). The entire cmake invocation is a SINGLE
  `- [...]` list item.
[CheckList]
- The recipe begins with a top-level `compile_db:` section (sibling of `targets:`, `path:` pointing at the build's compile_commands.json) — without it the recipe builds but NEVER publishes facts, and the publish step fails forever
- The build REUSES the project's own build entry (build.sh / test wrapper / the CI build command) when one exists, instead of a hand-reconstructed command string; the compile is routed through `arbiter cc` by the least invasive form that entry offers — a compiler-launcher hook (CMake: CMAKE_C/CXX_COMPILER_LAUNCHER), a CC/CXX prefix (make/autotools or a wrapper that honors them), or standalone shim scripts only as a last resort — keeping the project's own compilers; every form includes `--root ABS_REPO`
- recipe_search, then write .arbiter/recipes.yaml (build half: compile_db + a real cc-interposed src_compile stage + a bare test_run.cmd) in the shape above, then register {"path": ".arbiter/recipes.yaml"}
- Submit build-published from a REAL cc-interposed build that publishes the facts index — never a Bash build, a file-exists check, or a marker file; a recipe with no real src_compile stage publishes nothing
[Submit] build-published
[Branch]
success: prove
failure: derive

[STEP] prove
[StepJob]
The build is proven and the facts index published; now make the test actually RUN and prove the
suite end to end. Discover the runtime environment the test needs, add it to the recipe you wrote at
derive, and submit candidate-proven.
1. Discover what the test needs to RUN CORRECTLY, not merely compile — and make it RECIPE CONTENT so
   a clean checkout runs the test from the recipe alone. The setup a test assumes is rarely in the
   build files; look where the project documents how it RUNS its tests. START at the CI config — it is
   the AUTHORITATIVE record of how the project builds AND runs its tests on a clean machine — and only
   then fall back to docs and the test sources, in order: (a) the CI config
   (`.github/workflows/*.yml`, `.gitlab-ci.yml`, `azure-pipelines.yml`, a `Jenkinsfile`): the env vars
   it exports, the services/containers it starts, and the setup/bootstrap/env scripts it runs before
   the test step. When CI `source`s (or `.`-includes) an env/bootstrap script before testing, that
   script — not your shell — is where the test's environment comes from: OPEN it and read the
   variables it EXPORTS, because `source`-ing it inside a recipe `pre` does NOT persist into the test
   process (each stage command is a separate process; see `env:` below). Lift those exported variables
   into `env:` directly. (b) test/contributor docs and `scripts/`/`tools/` setup or env scripts, and
   any sample config the tests read; (c) the test sources themselves — env vars they read, config/data
   files they open (usually relative to a working directory), and fixture `SetUp()` prerequisites. Put
   each finding in the recipe field that carries it (the slots shown in the derive shape):
   - **Environment variables the test needs → the target's `env:` map.** Each stage runs its `pre`,
     `cmd`, and `post` as SEPARATE processes that share this `env:` map — so a variable set by
     `source some-env.sh` inside a `pre` command does NOT carry over to the test. If the project
     sources an env script, read the variables it exports and put them in `env:` directly.
   - **Services, data directories, config-file generation, one-time setup (any side effect beyond an
     env var) → `test_run.pre`.** Each `pre` item is one argv list; for a shell snippet (sourcing,
     `&&`) use `[sh, -c, "<snippet>"]`. `pre` runs before `cmd`; a non-zero `pre` fails the stage,
     which cleanly says "the environment broke," not "the test failed."
   - **The directory the test must run from → the target's `workdir:`** (tests often open data/config
     relative to cwd).
   Keep `test_run.cmd` the BARE binary — setup goes in `env`/`pre`/`workdir`, never folded into `cmd`
   (the harness appends `--gtest_*` to that argv). "Works only if your shell happens to have X set" is
   an INCOMPLETE recipe; a test failing on an unset variable or an unstarted service is a RECIPE
   defect, not a test defect.
2. Add what you found to the recipe — fill the `env:` / `test_run.pre` / `workdir:` slots you left
   blank at derive — re-register, then prove the suite by SUBMITTING candidate-proven: SubmitTask with
   result `{"verify": "candidate-proven"}`. The REFEREE runs the WHOLE suite (`tests: ["*"]`) for you;
   that submission advances the step. Do NOT try to satisfy this by calling the `run` tool yourself, or
   by building in Bash — only a submitted predicate counts. If you do call the `run` tool to
   sanity-check, its `tests` are gtest patterns (`Suite.Case`, `Suite.*`) or `["*"]` for the whole
   suite — never the test binary's filename, which matches no suite and returns `no_tests_ran`. The
   gtest harness injects its own `--gtest_output`; the recipe's `test_run` cmd is just the binary. A
   pass or a fail both prove the harness; an errored or zero-test run does not.
   A zero-test WHOLE-SUITE run (`no_tests_ran` on `tests: ["*"]`) is NOT an environment problem to fix
   here: it means the target you proved at derive has no test cases that run on this host (they are
   platform-guarded out at compile time, so the binary is empty). That target cannot be proven on this
   host — do not loop trying to fix it. This step's failure returns to derive; go back there and pick a
   different small target whose tests run on this host.
   candidate-proven passes on a `passed` OR a `failed` run, but a run that "failed" only because the
   environment from sub-step 1 was never set up is an INCOMPLETE recipe, not a result — even though it
   satisfies the predicate. Before treating a failing run as done, triage it: an environment-shaped
   failure — "connection refused" / cannot connect, "No such file or directory", an unset/empty env
   variable, permission denied, a fixture `SetUp()` failing, or EVERY test failing the same way in a
   fresh recipe — means the recipe is missing `env` / `test_run.pre` / `workdir`; add it to the recipe
   (this step's failure returns to derive, where you re-author with the env and re-prove) and record
   the trap in the target's `notes`. Only a genuine assertion outcome
   (`EXPECT_*` / `ASSERT_*` on the code, in an environment that is demonstrably up — e.g. sibling
   tests pass) is a real result. The dividing question: would this test pass for a developer whose
   machine is set up correctly? If yes, that setup was the recipe's job.
[CheckList]
- The recipe declares the runtime environment the test needs to RUN — env vars in `env:`, services/setup in `test_run.pre`, the right `workdir:` — discovered CI-config-FIRST (any env script CI sources is opened and its exports lifted into `env:`, not just named), then test docs / test sources, not guessed
- Submit candidate-proven from a real run (structured gtest output only) — never a file-exists check, marker file, or shell shortcut; an environment-shaped failure is a recipe defect to fix and re-run, not a reported test result
[Submit] candidate-proven
[Branch]
success: enumerate
failure: derive

[STEP] enumerate
[StepJob]
Prove the published snapshot is queryable and carries the project's test set — publication is not
searchability. Call scan {"scope": "*"} to pull the facts-derived test inventory (each gtest case
recorded as its generated `Suite_Case_Test` fixture type) and use it as the authoritative test
list for what you report — do not hand-list tests or recall the suite from memory. Then submit
tests-enumerated: the referee re-runs the `_Test` index query itself against the published
snapshot and passes only when the snapshot actually contains the test set, so an enumeration you
assert but the index does not contain cannot pass.
Then make the WHOLE SUITE runnable, not just the one target you proved. List every test binary the
build produces — `ctest -N`, the build's test/target list, or the test-executable declarations in
the build files — and register a recipe for EACH via import_recipes: one RecipeBook with a target
per binary, same shape as the proven one (its own build target + binary path + `gtest` harness,
reusing the `env`/`test_run.pre`/`workdir` that apply). register/import_recipes are pure writes, so
this is cheap and does NOT build anything: each added target stays UNPROVEN until its first `run`
(its tests enter the facts index only once it is built), exactly like a fresh recipe. Keep the
proven target's id `src_compile`; give the others their binary names. For a large suite, GENERATE
the book programmatically (loop the enumerated binaries into the target shape) rather than
hand-writing each. The goal: a clean checkout can `run` ANY suite from the committed book without
re-deriving. This step only REGISTERS the book (a recipe per binary); the NEXT step (cover) builds
and indexes those binaries so the whole project test suite enters the facts index.
[CheckList]
- Call scan {"scope": "*"} and treat its facts-derived set as the authoritative test inventory
- Confirm the snapshot answers a query (search/detail) before submitting — publication is not searchability
- Submit tests-enumerated (referee re-queries the index; your transcript is not the test set)
- Register a recipe for EVERY test binary the build produces (import_recipes — one target per binary, ids = binary names) so the whole suite is runnable, not just the single target you proved; these stay unproven until first run
[Submit] tests-enumerated
[Branch]
success: cover
failure: derive

[STEP] cover
[StepJob]
Now COVER the whole project test suite: build and index every test binary so the facts index
carries the project's tests, not just the one binary derive proved. This is the purpose of the
bootstrap — full coverage, so a clean checkout can run any suite AND the index knows every test.
Build the project's test targets THROUGH `arbiter cc` (the same compiler-launcher wiring from
derive), so every test translation unit is journaled and indexed. The facts index merges
INCREMENTALLY across builds, so you do NOT rebuild from scratch each time and you do NOT need a
separate run per binary: drive one (or a few) PARALLEL cc-interposed builds of the whole test tree
— the build's aggregate unit-test target, or the test subdirectory, with `-j` — and the index
accumulates every compiled test. You do NOT need the tests to PASS, only to BUILD and index (the
gate measures coverage of declared-vs-built, never pass/fail). Some binaries may not build on this
host (platform-guarded, or a broken unrelated TU); skip those and keep going — cover as much of the
suite as the host can build. When the index carries the suite, submit suite-covered: the referee
re-runs `discovery.coverage()` over the live snapshot and passes only at substantial project
coverage (built / declared, vendored third-party excluded). A run that indexed only the one derive
binary scores ~0 and fails; cover the suite to pass. If it fails, read the reported ratio, build
more of the suite, and submit again.
[CheckList]
- Built the project's test targets through `arbiter cc` (one or a few parallel builds of the whole test tree), so their tests entered the facts index — not one binary at a time, not a non-interposed build
- Skipped only the binaries the host genuinely cannot build (platform-guarded / unrelated breakage), covering as much of the suite as possible
- Submit suite-covered — the referee measures built/declared project coverage from the live index; report the ratio reached
[Submit] suite-covered
[Branch]
success: reconcile-perf
failure: cover

[STEP] reconcile-perf
[StepJob]
Prove perf-mcp on its REAL functions, not a version probe. Submit perf-static-scan: the referee
itself calls `perf.scan_c` over the project and requires a schema-versioned findings payload — that
proves the static analyzer actually runs and returns typed output (the findings list may be empty
on a repo with no C/C++ hot paths; that is still a pass). Then exercise the rest of the surface and
report what it returns: call `perf.measure_command` with a trivial command (e.g. argv ["true"]) and
confirm a `perf-mcp.measure.v1` timing payload, and call `perf.explain_finding` (pass a `finding`
from the scan, or a `rule_id`) and confirm a `perf-mcp.explain.v1` explanation. Report all three
results. Do not submit a toolchain probe in place of the scan — a version check is not a proof.
[CheckList]
- Submit perf-static-scan (referee runs perf.scan_c → schema-versioned findings)
- Call perf.measure_command on a trivial command and report the perf-mcp.measure.v1 timing
- Call perf.explain_finding on a scan finding or rule_id and report the perf-mcp.explain.v1 explanation
[Submit] perf-static-scan
[Branch]
success: reconcile-diag
failure: reconcile-perf

[STEP] reconcile-diag
[StepJob]
Prove gdb-mcp on REAL debugging, not just gdb_diagnostics, and exercise the remaining capability
tools. Submit gdb-debugs-real-binary: the referee compiles a tiny program and drives gdb through
break->run->inspect, asserting it reads a live value — it passes on hosts where gdb can debug, and
reports-and-passes where gdb is absent or the host forbids launching inferiors (a host limitation
never fails the repo). Alongside it, drive a real gdb-mcp session against your proven test binary
and report the outputs: gdb_start (mode exec on the binary) → gdb_breakpoint (set one) → gdb_exec
(run) → gdb_stack / gdb_eval (read a frame and a value) → gdb_stop. Also exercise the recipe-import
path — call import_recipes on a recipe book and confirm the imported recipe is queryable via
recipe_search — and append one NotePlaybook gotcha capturing anything you learned this run.
[CheckList]
- Submit gdb-debugs-real-binary (referee proves gdb debugs, or reports a host limitation)
- Drive a real gdb-mcp session (gdb_start → gdb_breakpoint → gdb_exec → gdb_stack/gdb_eval → gdb_stop) on the proven binary and report the outputs
- Exercise import_recipes (then recipe_search to confirm the import is queryable) and append a NotePlaybook gotcha
[Submit] gdb-debugs-real-binary
[Branch]
success: confirm
failure: reconcile-diag

[STEP] confirm
[StepJob]
This is a human-confirmation gate — there is no executor task here. Do NOT CreateTask. Put the
`[Checkpoint]` question below to the user verbatim with AskUserQuestion (pass / fail options),
relaying the reported recipe id, snapshot id, test inventory, the perf scan/measure/explain
results, the gdb session/debug result, and any suggested facts.key_flags so they can judge. Then
call SubmitCheckpoint with their actual choice — pass advances to END, fail loops back to
reconcile-diag. Never decide on the user's behalf in an interactive run. ONLY in a
non-interactive / headless run where AskUserQuestion cannot reach a person (it errors or returns no
usable answer) do you SubmitCheckpoint {"decision":"pass"} once without a human — every prior step
was already referee-verified — and note the auto-approval in the report.
[Checkpoint]
The bootstrap proved the recipe, published facts, enumerated the test set, and exercised perf-mcp
and gdb-mcp on their real functions. Review the reported recipe id, snapshot id, test inventory,
perf scan/measure/explain results, gdb session/debug result, and any suggested facts.key_flags —
do these proven surfaces and the reported results look right for this repository?
[Branch]
success: END
failure: reconcile-diag
