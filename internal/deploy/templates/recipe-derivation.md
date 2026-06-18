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

[Verify] tests-enumerated
fact: _Test
expect: {"min_results":1}

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
Do these in order. Do not skip a sub-step, and do not fake any of them — the only thing that
finishes this step is a real registered recipe proven by a real run.
1. Probe the native build system (read its build files) to learn the configure command, the build
   command, the test binary it produces, the globs of sources it compiles, AND the project's real
   C and C++ compilers (the ones the build would use — e.g. what `cc`/`c++` resolve to, or what the
   build files name).
2. Wire that build through `arbiter cc` so every translation unit is journaled — that journal is
   what the facts index is built from. The compilers in your configure command must be the two shim
   scripts `.arbiter/shim_cc.sh` and `.arbiter/shim_cxx.sh`:
   - If they already exist, use them as-is — an operator may have pre-wired an unusual toolchain;
     do NOT edit or replace them.
   - If they do NOT exist, CREATE them now from the compilers you just probed. Write each as a
     one-line wrapper that routes the real compiler through `arbiter cc`, then `chmod +x` both:
       `.arbiter/shim_cc.sh`  contains:  `#!/bin/sh` (newline) `exec arbiter cc --root ABS_REPO -- REAL_CC "$@"`
       `.arbiter/shim_cxx.sh` contains:  `#!/bin/sh` (newline) `exec arbiter cc --root ABS_REPO -- REAL_CXX "$@"`
     substituting ABS_REPO = the repository's absolute path, REAL_CC / REAL_CXX = the compilers the
     build ITSELF uses by default — what `cc`/`c++` resolve to, or the `$CC`/`$CXX` the build sets,
     as found in step 1. Wrap THAT compiler even if it is not Clang: arbiter re-parses the journaled
     translation units into the facts index with its OWN capable Clang internally, so the build
     compiler does NOT need to be Clang for facts to publish — the "capable Clang" requirement is on
     arbiter's index, never on your build. Do NOT switch the build to a different compiler than it
     normally uses just because another one is installed; the shim must journal the project's REAL
     build, and wrapping a compiler the build would not have chosen makes the index describe a build
     that never happens. The wrapper MUST invoke `arbiter cc` (it is on PATH) — a direct compiler is
     not journaled, so facts never publish.
   Reference the shims as the compilers in your configure command by their **absolute** path — a
   build system that probes the compiler from a temporary directory (cmake does) cannot resolve a
   relative compiler path.
3. Write `.arbiter/recipes.yaml` (RecipeBook v2) in exactly the shape below — two-space indent, no
   tabs, lists inline [a, b, c] — wiring the two shim scripts as the compilers in the configure
   `pre` command. Strict YAML subset: NO anchors/aliases (`&`/`*`) and NO extra keys; every path
   (`compile_db.path`, `binary`, `sources`) is REPO-RELATIVE (no leading `/`). Set `binary` to the
   FULL repo-relative path the build actually writes the test binary to — including its build
   directory, e.g. `build/<name>`, NOT just `<name>`. It lets arbiter skip an unchanged rebuild,
   so the publish step reuses the cached build (fast) instead of recompiling; a `binary` that does
   not point at the real output file disables that cache and makes the publish snapshot incomplete.
   Then call
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
   builds nothing — so the `_Test` index stays empty and the next `enumerate` step fails forever
   (facts never publish, because `arbiter cc` was never invoked). The cc-interposed build at this
   step is what publishes the first facts snapshot; do NOT drop, empty, or comment out the
   `src_compile` stage to get a green candidate-proven — the build stage is the point.

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
- The recipe begins with a top-level `compile_db:` section (sibling of `targets:`, `path:` pointing at the build's compile_commands.json) — without it the recipe builds but NEVER publishes facts, and the publish step fails forever
- The configure command wires .arbiter/shim_cc.sh and .arbiter/shim_cxx.sh as the compilers (real compiler through arbiter cc); create them from the probed compiler if they are absent, or use them as-is if an operator already wired them
- recipe_search, then write .arbiter/recipes.yaml in the shape above, then register {"path": ".arbiter/recipes.yaml"}
- Submit candidate-proven from a real run (structured gtest output only) — never a file-exists check, marker file, or shell shortcut
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
[CheckList]
- Call scan {"scope": "*"} and treat its facts-derived set as the authoritative test inventory
- Confirm the snapshot answers a query (search/detail) before submitting — publication is not searchability
- Submit tests-enumerated (referee re-queries the index; your transcript is not the test set)
[Submit] tests-enumerated
[Branch]
success: reconcile-perf
failure: derive

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
