# Proposal: split `derive`, combine native+cc into one `gear-up`, and absorb 4 rsg conventions

Status: **landed** — merged to main as PR #131 (current HEAD). The prose below is the as-built
plan.

Builds on the PR #130 `derive → prove` split. Relates to ADR-0004 (build-driven
indexing — "compile done ⇒ index done"), ADR-0017 (result integrity: curated,
step-bound predicates), ADR-0020 (mandatory index hard stop), and the named
`[Verify]` predicate mechanism in `verify-predicates-and-comments.md`.

## Motivation

Today the `recipe-derivation` opening's `derive` step is one gate
(`build-published`) sitting behind **four** jobs — probe the build, wire
`arbiter cc`, author the recipe, publish facts. A red gate localizes nothing,
and the model is asked to hold all four concerns at once.

The original sketch was a 4-step re-slice: (1) make the app run natively, (2)
run it under `arbiter cc` and boot with no error, (3) scan for all tests, (4)
build & prove all tests. Steps 3–4 already exist as `enumerate`/`cover`/`prove`.
Steps 1–2 are the substance — but a literal reading makes them *weaker* gates
than the one they replace:

- **A standalone no-cc "native build" gate is more gameable, not less.** It
  re-runs a submitter-supplied command string and trusts a submitter-supplied
  `binary:` path, *before any unforgeable evidence exists*. `build_cmd:"true"`,
  `binary:"<any host gtest binary>"` passes it. It also leaves no durable
  arbiter artifact (no facts index).
- **"boot returns exit 0" alone proves almost nothing.** A gtest binary under a
  no-match filter exits 0 having run nothing; `cmd:[true]` exits 0; a binary
  that catches its own SIGSEGV exits 0.

This proposal keeps the intent of steps 1–2 and corrects the shape. The native
run is **demoted from a gate to an un-gated sub-step**, and the only thing that
counts as progress is the ungameable cc-interposed evidence. Steps 1 and 2 are
**combined into a single `gear-up` step** so the two-phase "native, then cc"
model is preserved without re-bloating it. Net pipeline: **7 steps**.

```
gear-up → scan-tests → cover → prove → reconcile-perf → reconcile-diag → confirm
```

| Step | One objective | `[Verify]` predicate | On fail |
|---|---|---|---|
| **gear-up** *(was derive; native+cc combined)* | the cc-interposed build publishes facts **and** the binary it produced boots+enumerates | `build-booted` *(new)* — `facts.published==true` **and** `boot.exited_zero && boot.listed_tests≥1` | self-loop; failure msg compares native-vs-cc |
| **scan-tests** *(was enumerate)* | full declared test set enumerated; every binary registered | `tests-enumerated` *(unchanged)* | self-loop |
| **cover** | a real cc-run drives per-binary coverage of AST-declared files ≥ 0.50 | `suite-covered` *(unchanged)* | self-loop |
| **prove** *(re-ordered after cover, thinned)* | whole suite asserts in its runtime env (genuine `passed`\|`failed`) | `candidate-proven` *(unchanged)* | dual: `no_tests_ran`→gear-up; env-shaped→re-author env |
| reconcile-perf / -diag / confirm | *(unchanged)* | perf / gdb / `[Checkpoint]` | — |

## (A) Steps

### Step 1 — `gear-up` (combines the sketch's step 1 + step 2)

**One objective:** the `arbiter cc`-interposed build produces a **working,
indexed binary** — it publishes the facts index *and* the binary it built links,
loads its shared libraries, and is a genuine gtest binary that enumerates ≥1
case. Runtime-environment discovery and proving the suite *passes* are NOT here
(they remain `prove`).

The step has two sub-steps, only the second of which is a gate:

- **Sub-step A — native smoke (UN-GATED, rsg-style).** Build the project with
  its own build entry and run `<binary> --gtest_list_tests` to exit 0, with no
  `arbiter cc` in the loop. This is never submitted, so there is no verdict to
  fake. It earns its place three ways: (1) **de-risk** — confirm the project
  builds on *this host* before wiring the launcher; (2) **discovery** — running
  it natively is how the model learns the configure/build commands, the binary
  path, and the source globs it needs to author the cc recipe (rsg's "execute
  first, then write the harness"); (3) **diagnostic** — "native ran, cc didn't"
  ⇒ the launcher wiring is wrong; "native also failed" ⇒ the project/target is
  the problem. This is the project-broken-vs-cc-broken split the standalone gate
  was rejected for, now grounded in an actual native run instead of guessing
  from cc-build stderr.

- **Sub-step B — interpose + author + submit (THE GATE).** Wire `arbiter cc` as
  a launcher (least-invasive form), write the build-half recipe (`compile_db:` +
  a real `src_compile` target + a bare `test_run.cmd`), `register`, then submit
  `build-booted`. The referee builds `src_compile` (publishing facts from the
  journaled TUs) and runs `<binary:> --gtest_list_tests` itself, reading *its*
  exit code and *its* listed-test count.

**Why native-run is a sub-step, not a gate.** The critics' objection was narrow:
a *submitted* native build re-runs a submitter command with no facts anchor, so
`build_cmd:"true"` / a borrowed host binary passes it. As an un-gated sub-step
there is nothing to submit and nothing to fake — the only gate is the
cc-interposed `build-booted`, anchored to `facts.published` (libclang-built from
the real TUs, unforgeable) and to a `--gtest_list_tests` count the referee reads
off the same binary the cc build produced.

**Why combining does not revive "overwhelmed derive".** The open-ended,
heavy part of the old `derive` — runtime-environment discovery and proving the
suite runs — already lives in `prove` (PR #130). What remains in `gear-up` is
**bounded**: the native smoke and the boot enumeration are cheap and mechanical,
and "wire cc + author the build-half" is unavoidable in *any* step that produces
the cc build. So the two-phase model is preserved in one coherent step without
re-absorbing the heavy concern.

**[Branch] on fail:** self-loop `gear-up`. The failure message surfaces *which
clause* failed (`facts.published` vs `boot`) plus the native-vs-cc comparison
from sub-step A. Because `facts` publishes from the compile and `boot` reads the
linked binary, a `facts ✓ / boot ✗` report localizes precisely to "compiled and
indexed, but the **link** failed."

### Step 2 — `scan-tests` (renamed from `enumerate`)

Rename only; predicate `tests-enumerated` unchanged. The referee re-runs the
`_Test` index query against the published snapshot, and the AST-declared set is
computed by the referee's own `scan.py` walk — the submitter's transcript is not
the test set.

### Step 3 — `cover` (unchanged)

`suite-covered`: the referee re-runs `discovery.coverage(".")` and passes on a
built/declared ratio ≥ 0.50. Built files come only from a real cc-run (a wall of
shell-built binaries scores zero); declared comes from the build-independent AST
scan; vendored excluded.

### Step 4 — `prove` (KEPT, RE-ORDERED to after `cover`, THINNED)

**One objective:** the whole suite asserts on the code in its discovered runtime
environment (a genuine `passed`|`failed`). Predicate `candidate-proven`
unchanged (`run: src_compile`, `tests: ["*"]`,
`expect: {"overall":{"one_of":["passed","failed"]}}`).

Thinned because `gear-up`'s boot clause already catches link/load/static-init
failures, leaving only **fixture-level** env discovery (a `SetUp()` that connects
to a DB is not exercised by `--gtest_list_tests`). The dividing question stays:
*"would this test pass for a developer whose machine is set up correctly?"* — an
env-shaped failure (connection refused, missing file, unset var, every test
failing identically) is a recipe defect, not a result.

**[Branch] on fail — two routes:** a `no_tests_ran` / whole-suite-empty outcome
→ `gear-up` (the target is platform-guarded-empty on this host; re-pick); an
env-shaped failure → re-author `env:`/`test_run.pre:`/`workdir:` and re-prove.
Since `[Branch]` selects one target, route `failure: gear-up` as the safe
superset (gear-up's probe re-selects the target and subsumes re-authoring env),
with the StepJob prose distinguishing the two.

**boot vs prove boundary:** document that boot caught link/load/static-init, NOT
fixture env, so the model does not over-trust a green boot.

### Steps 5–7 — `reconcile-perf`, `reconcile-diag`, `confirm` (UNCHANGED)

`reconcile-perf` / `perf-static-scan` (mcp gate), `reconcile-diag` /
`gdb-debugs-real-binary` (shell gate, reported-not-gated, also the END-side
`NotePlaybook` gotcha sink — distinct from recipe `notes:`), and `confirm`
(`[Checkpoint]`, human gate) are unchanged. `confirm` *should* additionally relay
each target's `notes:` to the human (see open questions).

## (B) Before → after

| Today | Becomes |
|---|---|
| `derive` — one gate (`build-published`) behind four objectives | `gear-up` — un-gated **native smoke** sub-step (de-risk + discovery + diagnostic) followed by the cc gate `build-booted` (facts **and** boot). Native, then cc, in one step. |
| *(sketch step 1)* a standalone no-cc native-build **gate** | **Rejected as a gate** (no facts anchor, submitter-supplied cmd/binary, no durable artifact) — **preserved as `gear-up`'s un-gated native-smoke sub-step.** |
| *(sketch step 2)* a standalone `boot` step | **Folded into `gear-up`** as the second clause of `build-booted`, made ungameable by `exited_zero AND listed_tests≥1` from a referee-run `--gtest_list_tests`. |
| `build-published` asserts only `facts.published` | `gear-up` submits a **new local** predicate `build-booted` = `{facts.published, boot}`. The siblings (`freeplay`/`gold-digger`/`regression-triage`/`playbook-create`) keep `gear-up-published` unchanged — no boot clause there. |
| `enumerate` / `tests-enumerated` | `scan-tests` — **rename only**; predicate unchanged. |
| `prove` carries all runtime-env discovery + full env-trap triage | `prove` **kept, re-ordered after `cover`, thinned** (boot pre-catches link/static-init), dual fail-branch. |
| `cover`, `reconcile-perf`, `reconcile-diag`, `confirm` | unchanged (`confirm` should additionally relay `notes:`). |

## (C) Drop-in template text

**`build-booted` predicate (top-of-file `[Verify]` block):**

```
# The gear-up gate: the cc-interposed build PUBLISHED the facts index AND the binary it
# produced boots. Two clauses, both required, on one no-match run:
#   facts.published   — libclang indexed the journaled TUs (unforgeable; a Bash/own-run build
#                       or an emptied src_compile leaves this false).
#   boot.exited_zero + boot.listed_tests_min:1 — the referee runs `<binary:> --gtest_list_tests`
#                       itself; the binary links, loads its .so's, and enumerates >=1 case. Closes
#                       the cmd:[true]/echo cheat (exits 0, lists ZERO) without requiring any test
#                       to PASS (that is prove). No "overall" clause — a no-match run is errored;
#                       Pass() honors the typed Verdict first (verify.go:128-150).
[Verify] build-booted
run: src_compile
tests: ["__arbiter_boot__"]
expect: {"facts":{"published":true},"boot":{"exited_zero":true,"listed_tests_min":1}}
```

**Amended recipe target shape — add `notes:` as a single-line DOUBLE-QUOTED scalar (NOT a block scalar):**

```
    targets:
      - id: src_compile
        binary: build/TEST_BINARY
        harness:
          kind: gtest
        workdir: .                          # dir the test runs from (filled at prove); OMIT to default to repo root
        env:                                # env vars the test needs to run (filled at prove); OMIT the whole key if none
          SOME_VAR: some-value
        notes: "trap: <symptom> -> cause: <root cause> -> fix: <recipe field> | <next trap>"   # one clause per runtime trap you hit-and-fixed at prove; OMIT if first try
        src_compile:
          pre:
            - [cmake, -S, ., -B, build, "-DCMAKE_C_COMPILER_LAUNCHER=arbiter;cc;--root;ABS_REPO;--", "-DCMAKE_CXX_COMPILER_LAUNCHER=arbiter;cc;--root;ABS_REPO;--", -DCMAKE_BUILD_TYPE=Debug]
          cmd: [cmake, --build, build, --target, TEST_BINARY]
        test_run:
          pre:                              # runtime setup (filled at prove); OMIT if none
            - [sh, -c, "./scripts/setup-test-env.sh"]
          cmd: [./build/TEST_BINARY]        # the BARE binary only — setup goes in env/pre/workdir, never here
        sources: [SRC_GLOB_1, SRC_GLOB_2]
```

**`gear-up` `[STEP]` block:**

```
[STEP] gear-up
[StepJob]
Produce a working, indexed binary: the arbiter-cc-interposed build PUBLISHES facts AND the binary it
builds boots. Two sub-steps; do them in order.

A) NATIVE SMOKE (no arbiter cc, NOT submitted). Build the project with its OWN build entry
   (build.sh / run_tests.sh / a make target / the CI build command) and run `<binary> --gtest_list_tests`
   to exit 0. This de-risks (does the project build on THIS host?), discovers the real configure/build
   commands + binary path + source globs you will wire next, and — if cc later fails — tells you whether
   the project or the launcher is at fault. REUSE the project's entry; do not hand-reconstruct it.

   Negative build-file claims are HYPOTHESES. If a target is gated ("requires X", "not built on this
   platform", a disabled-by-default option), do not abandon it untried. Three legal moves only: (1) READ
   the gate; (2) if it is an untried assumption, satisfy it the PROJECT'S OWN documented way (-DOPT=ON,
   a documented env var, a named dependency) — this changes only your invocation, never a tracked file;
   (3) if it is a real host limitation, pick a DIFFERENT target and record why via NotePlaybook. Editing
   the guard / #if, or stubbing the dependency, is NEVER legal — arbiter evaluates the project, it does
   not patch it. Prefer a smaller unconditional target over flipping a knob; "install the dependency" is
   best-effort (may be impossible offline) and never step-blocking.

B) INTERPOSE + AUTHOR + SUBMIT. Wire arbiter cc as a LAUNCHER (least-invasive form:
   CMAKE_*_COMPILER_LAUNCHER; else CC=/CXX= prefix; else a shim as last resort; always --root ABS_REPO,
   always wrapping the REAL compiler). Write the BUILD half of .arbiter/recipes.yaml (compile_db: + a real
   src_compile target + a bare test_run.cmd; leave env:/workdir:/test_run.pre/notes: for prove). register,
   then SUBMIT build-booted. The REFEREE builds src_compile (publishing facts) and runs
   `<binary:> --gtest_list_tests` itself — exit 0 AND >=1 listed case proves the cc-built binary links and
   enumerates, WITHOUT requiring any test to pass (that is prove).
[CheckList]
- RED FLAG self-check (the REFEREE re-builds and is the only gate; this list only tells you when NOT to waste a submit): do NOT submit if your recipe has no real src_compile stage, or you emptied/dropped/commented it to force a green — a recipe with no cc-interposed compile publishes NO facts.
- RED FLAG: do NOT submit a build you made outside this predicate (a run call you made, or a Bash/make/cmake build in your own shell) — a snapshot built outside it leaves facts.published=false no matter how many times you build.
- RED FLAG: do NOT submit a file-exists check, a marker file, or any shell shortcut as the build proof; confirm the compile is routed through arbiter cc (LAUNCHER / CC= / shim) or nothing is journaled.
- RED FLAG: a binary that exits 0 but lists ZERO tests is not a real gtest binary (cmd:[true]/echo) — boot requires >=1 listed case; pick the binary src_compile actually built.
- Native smoke ran first: you built with the project's own entry and `<binary> --gtest_list_tests` exited 0, so you discovered the real build cmds/binary/globs and confirmed the project builds on this host.
- REUSE the project's own build entry; choose the SMALLEST self-contained test target, not an aggregate.
- binary: is the artifact src_compile builds; a target that lists ZERO cases is platform-guarded-empty on this host — pick a different target now.
[Submit] build-booted
[Branch]
success: scan-tests
failure: gear-up
```

## (D) The 4 absorbed rsg conventions

Each absorption had a blocking parser/schema bug the adversarial review caught;
the corrected form is what is shown here.

### (1) `notes:` in the recipe shape — one-line template fix (no engine change)

`notes` already exists end-to-end in the engine: parsed (`recipes.py:275`),
serialized (`recipes.py:147-148`), in `TARGET_KEYS`, surfaced + searched in
`recipe_search` (`rpc/__init__.py:608-617`). The only defect is that the
author-facing shape never lists it, yet `prove` orders writing it. **Fix:** add
the `notes:` line in (C), and patch the placeholder prose to name `notes:` as a
prove-time slot. **Blocking:** it must be a **single-line double-quoted scalar**,
not `notes: |` — the recipe parser is a hand-written subset
(`recipes.py` `_parse_yaml_subset`) with no block-scalar support and rejects the
block form (`indentation jumps more than one mapping level`). A double-quoted
single line parses, round-trips, and protects embedded `#` / `->` / `|`.

### (2) Per-trap incremental notes (rsg "every error → every fix") — prompt fix (no engine change)

At `prove`, every derive↔prove loop iteration that fixes an env-shaped failure
appends ONE clause to that target's `notes:` in the fixed form
`trap: <symptom> -> cause: <cause> -> fix: <recipe field>`, joined with ` | `
inside the one quoted string. **Persistence rationale (corrected):** notes
persist because **the author's own `.arbiter/recipes.yaml` on disk is the
deliverable** — `register` only parses/validates (`rpc/__init__.py:626-629`), it
does not write the file. Mid-loop edits are pin-safe (`recipes_pin.go:70` allows
book drift for `capabilities:[recipes]` openings). Not gameable: `notes:` is
asserted by no predicate. Library-safe: omitted when first-try-clean.

> Prove StepJob append: *"As you fix EACH env trap, append one clause to the
> target's `notes:` (`trap: symptom -> cause -> fix`), joined with ` | `; edit
> your recipes.yaml and re-register to re-validate (register validates, it does
> not commit — your file on disk is the deliverable). notes: is documentation,
> never a gate; a first-try-clean suite needs none."*

### (3) Consolidated red-flags self-audit (rsg "about to ship the wrong thing") — template fix, parser-driven form

**Blocking:** the obvious nested-bullet block does NOT parse. Under `[CheckList]`,
`firstToken` strips leading whitespace (`parse.go:798`), so every indented `- `
sub-bullet becomes a sibling top-level item, and any non-`- ` line (a trailing
"if any of these is true…" sentence, or an HTML comment) emits
`IssueStrayContent` and **fails the playbook load** (`parse.go:291-293`).
**Therefore each red flag is ONE single physical `- ` line** — no nesting, no
trailing prose, no comment marker. Lead each with the "the referee is the only
gate" framing so trimming cannot flip the meaning. The drop-in items live in
`gear-up`'s CheckList (shown in (C)) and a mirror set in `prove` (adding the
env-shaped-failure and `no_tests_ran` flags). Carry-forward: **trim the
now-duplicated inline StepJob copies to pointers** so the heuristics do not
appear twice — the goal is less load, not more text.

### (4) Bounded "negative build-file claims are hypotheses" clause — template fix (no engine change)

Lives in `gear-up` sub-step A (native smoke / target selection), shown verbatim
in the `[STEP]` block in (C). rsg's "every negative claim is a hypothesis" with
its **patch-the-gate conclusion amputated**: three legal moves (read the gate /
satisfy it the project's own documented way / pick a different target); editing
the guard or `#if`, or stubbing the dep, is never legal. **Fixes:** records
move-3's reason via the already-wired `NotePlaybook` gotcha
(`reconcile-diag` L406-410), not `notes:`, so the clause is self-contained and
does not depend on improvement (1) landing; and move-2 is bounded (prefer a
different unconditional target; "install the dependency" is best-effort, never
step-blocking, given the `-mod=vendor` offline posture). Honest label: this
hardens `gear-up`'s heaviest sub-step; it does not by itself reduce load.

## (E) Required engine / schema changes

The boot datum is the one real code change. Today the binary's process exit code
is collapsed into the test verdict and discarded (`gtest.py:231-249`); there is
no datum a boot clause can read.

- **`engine/arbiter_engine/runs/gtest.py`** — add `boot_exit_code: Optional[int]`
  and `listed_tests: Optional[int]` to `RunResult` (`gtest.py:30-42`); emit both
  in `to_json` when non-None. Add an explicit `<binary:> --gtest_list_tests`
  enumeration subprocess and stamp its exit + parsed count onto the RunResult
  alongside facts. **Blocking ordering fix:** surface `boot_exit_code` on
  **every** `run_target` return path (`L194,206,220,238,257,272`), not just the
  no-match path — the `exit_code != 0` check at `gtest.py:231` fires *before* the
  no-match path at `:250`, so a binary that **crashes on boot** (the case boot
  most wants to catch) would otherwise never reach the datum and `CompareRun`
  would see nil. Do NOT reuse the filtered run's exit (a no-match filter exits 0
  trivially).
- **`internal/verify/typed.go`** — `RunExpect.Boot *BootExpect` with
  `BootExpect{ExitedZero *bool; ExitCode *int; ListedTestsMin *int}`; include
  `Boot` in `ParseRunExpect`'s ≥1-clause guard (reject negatives); add
  `BootExitCode *int`, `ListedTests *int` to `RunEvidence`; add a nil-safe,
  fail-closed boot branch to `CompareRun` (OK iff
  `ev.BootExitCode != nil && *ev.BootExitCode==0 && ev.ListedTests != nil && *ev.ListedTests>=min`).
  `strictDecode` is key-closed, so a new clause must live in `RunExpect` or it is
  rejected. `build-booted` asserts **both** `facts.published` and `boot` in one
  `expect` — `CompareRun` already evaluates all clauses and reports per-clause;
  no special-casing needed. `CompareRun` sets `result.Verdict`, which `Pass()`
  honors before the failure code (`verify.go:128-150`) — required since the
  engine stamps `failure=no_tests_ran` on exactly this run.
- **`internal/verify/verify.go`** — add `BootExitCode *int json:"boot_exit_code"`
  and `ListedTests *int json:"listed_tests"` to the `runRun` payload struct
  (`verify.go:375-418`); copy into the `RunEvidence` literal (`verify.go:410`).
- **`internal/match/verify_named.go`** — NO change needed (corrected during
  implementation). The design assumed `boot` would be a top-level `ResultSpec`
  field; in fact it lives inside `spec.Expect` (the run-expect JSON), already
  covered by the existing `expect` case in `inlineVerifyField`.
- **`engine/arbiter_engine/runs/recipes.py`** — REFERENCE-ONLY, do not edit.
  `notes` is already fully wired; an edit risks breaking the frozen-dataclass
  round-trip.

## (F) Open questions

1. **Combine vs split (this proposal's central choice).** Recommended: ONE
   `gear-up` step with the `build-booted` two-clause gate (preserves the
   native+cc model, fewest steps). The alternative — keep `gear-up`
   (`build-published`, facts only) and `interpose` (`boot-clean`, boot only) as
   two gates with the native smoke as `gear-up`'s lead-in — buys slightly
   stronger failure isolation at the cost of an extra step; the per-clause report
   already recovers most isolation. Confirm the combined shape.
2. **Predicate naming.** Recommended `build-booted` (new, local to
   `recipe-derivation`) so the shared `gear-up-published` in siblings is
   untouched. Alternative: keep `build-published` and add boot only in this
   opening (diverges a shared name's meaning — not recommended).
3. **Library / empty degradation.** A target whose cases are all `#if`-guarded
   out lists ZERO and fails boot early at `gear-up`. Confirm early-fail-and-repick
   is desired vs letting it through to `prove`'s `no_tests_ran` path.
4. **`prove` dual fail-branch.** Single `failure: gear-up` superset with prose
   distinguishing env-shaped from `no_tests_ran`, or two explicit branches?
5. **Surface `notes:` at the `confirm` checkpoint** so the human gate sees
   per-target env traps, or keep author-facing only?
6. **Identifier churn.** Approve `derive → gear-up` and `enumerate → scan-tests`
   (requires updating every `[Branch]` target pointing at `derive`/`enumerate`,
   plus any e2e launch recipe keyed on step ids), or keep step ids to minimize
   churn?
7. **Back-port** the boot clause + flat red-flags convention to the sibling
   openings (which share `gear-up-published` but have neither), or scope to
   `recipe-derivation` only?
8. **Boot's bounded guarantee (security note from code review).** `boot` proves
   `binary:` links, loads its `.so`s, and enumerates ≥1 gtest case — it does NOT
   prove that binary was produced by *this* build, nor that it is a compiled ELF.
   The engine runs `<binary:> --gtest_list_tests` against the recipe-declared path,
   independent of the `src_compile` translation units that `facts.published` indexes,
   and a shell script printing indented lines passes the enumeration. So
   `build-booted`'s integrity is carried by the CO-ASSERTED `facts.published` clause
   (a real, unforgeable cc-interposed compile) plus the downstream `tests-enumerated`
   index re-query — a submitter who games `boot` with an unrelated binary still fails
   those. Accept this bounded scope (don't over-claim "the binary works"), or add an
   engine binding in a follow-up (e.g. require `binary:` to be the artifact
   `src_compile` actually wrote). As implemented, the boot enumeration is also gated
   to the `build-booted` no-match filter (`gtest.py` `BOOT_FILTER`) so `cover`/`prove`
   runs don't pay for an unread datum.

## (G) Migration / file-touch list

- `internal/deploy/templates/recipe-derivation.md` — collapse `derive` (and the
  considered standalone `interpose`/native-build steps) into ONE `gear-up` step:
  an un-gated native-smoke sub-step (A) + the cc author/submit sub-step (B)
  gated by the new `build-booted` predicate `{facts.published, boot}`; rename
  `enumerate → scan-tests`; re-order `prove` after `cover`, thin it, dual
  fail-branch; add `notes:` (single-line quoted) to the shape + per-trap prose at
  prove; add flat single-line red-flag CheckList items at `gear-up` + `prove` and
  trim inline duplicates to pointers; add the bounded hypotheses clause at
  `gear-up` sub-step A (records via `NotePlaybook`); update every `[Branch]`
  target that pointed at `derive`/`enumerate`.
- `engine/arbiter_engine/runs/gtest.py` — boot datum: `boot_exit_code` +
  `listed_tests` on RunResult/`to_json`; a real `<binary:> --gtest_list_tests`
  subprocess stamping exit/count on EVERY return path (respect the
  `:231`-before-`:250` ordering).
- `internal/verify/typed.go` — `RunExpect.Boot` + `BootExpect`; `ParseRunExpect`
  guard; `RunEvidence.BootExitCode`/`ListedTests`; nil-safe fail-closed
  `CompareRun` boot branch.
- `internal/verify/verify.go` — decode `boot_exit_code`/`listed_tests` in the
  `runRun` payload; copy into the `RunEvidence` literal (`verify.go:410`).
- `internal/match/verify_named.go` — NO change (boot is nested in `spec.Expect`,
  already covered; the design's assumption that it was a top-level field was wrong).
- e2e launch recipes / fixtures / tests — grep `templates/`, `engine/tests/`, and
  headless e2e scripts for the step/predicate names `derive`, `enumerate`,
  `build-published` *in the recipe-derivation context* and update in lockstep
  with the renames. The siblings' `gear-up-published` is unchanged.
- `engine/arbiter_engine/runs/recipes.py` — REFERENCE-ONLY; `notes` already
  wired. Listed to prevent a redundant change.
