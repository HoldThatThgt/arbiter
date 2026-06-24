# engine-runs — `engine/arbiter_engine/runs/`

## Identity
crun-mcp rebuilt on the engine chassis: committed recipes, proven/unproven epistemics, structured
per-test results, census-validated build cache. **The doctrine ports; the surface is rewritten**
stdlib-only (FastMCP/pydantic/PyYAML dropped). gtest is first-class (ADR-0003); the harness seam
exists from day one but other adapters are M8+ behind their own ADRs.

## Inherits (semantics + tests, re-expressed in stdlib)
crun's proven-verdict epistemics (a run that compiles+launches+parses structured output is the
sole proof of a recipe; doc-only edits preserve proven), structured-output-only invariant
(per-test results come ONLY from injected result files, never stdout scraping), pre/cmd/post
stage semantics, recipe portability rules, docstring-as-playbook tool descriptions, the hermetic
fake-harness test strategy (fake compilers + fake gtest binaries emitting real XML), redaction
(length-only payload summaries).

## RecipeBook v2 (`.arbiter/recipes.yaml`, committed, strict YAML-subset)
```
vars: {...}                                 # RESERVED/INERT: parsed and round-tripped but never
                                            # expanded or consumed — no ${var} substitution exists
profiles: {debug:{...}, asan:{cflags_append:[-fsanitize=address,...], env:{ASAN_OPTIONS:...}},
           coverage:{...}}                  # named overlays on compile stages
compile_db: {path, target?}                 # journaled by `arbiter cc`; target = explicit
                                            # generator fallback when interposition is off
targets:
  - id, binary, tests, workdir, env
    harness: {kind: gtest, ...}             # adapter selector + adapter options
    sources: [globs]                        # census scope for cache/memo; no sources ⇒
                                            # no cross-process cache hits, ever
    requires, notes
    src_compile / test_compile / test_run   # stages; src_compile is shim-injected
```
Run options namespace harness specifics as `harness_options.gtest.*` (schema stays
harness-neutral for future adapters).

## Design
- **Runner**: stage execution under `build/<sha8(workdir)>.lock` (all engine children, including
  player goal runs — concurrent make in one tree is never allowed, ADR-0009); profile overlays
  applied to compile stages; `arbiter cc` injected into src_compile env; per-target config
  reload fixed (crun bug).
- **gtest adapter**: inject `--gtest_output=xml:<run_dir>/<target>.xml` (XML only — no JSON
  variant; JSON-output keys are rejected `invalid_args`); parse the result file only; per-test
  rows with `occurrence` column (repeated test names); typed
  `RunResult{overall, passed, failed, skipped, per_test[], run_id, facts?}`.
- **Build cache** (correct polarity, ADR-0005): SQLite `compile_cache{key, sources_digest,
  binary, built_at}`; a hit requires stage-key match AND clean census over `sources:` globs
  (direct work-tree scan, new/deleted detection); misses on any doubt. Survives restarts;
  identical cross-target `src_compile` builds once.
- **State** (`.arbiter/runs/state.sqlite`, WAL + busy_timeout): `scanned_test`,
  `run(+match_id,task_id,round)`, `run_test(+occurrence)`, `run_payload`, `target_state`
  (proven lifecycle RMW under `BEGIN IMMEDIATE`), `compile_cache`. Artifacts under
  `runs/<run_id>/`, returned as paths with capped `stdout_tail`.
- **guidance[]**: on failure, look up failing test symbols in the facts read_index and return
  ≤4 copy-paste `search`/`detail` next-queries with file:line — the red-test→facts loop.
- **Test discovery**: facts-derived (read_index query for the gtest fixture `_Test` *type* facts
  — the extractor does not emit a `::TestBody` method, so discovery keys off the generated
  `Suite_Name_Test` type) when a snapshot exists; tree-sitter only via the optional `[scan]`
  extra for cold repos, fail-closed when absent.
- **Adjudicated registration**: `register`/`scan` are capability-gated tools
  (go-seat); a recipe enters the committed book only after a refereed
  `{kind:"run", expect:{overall:one_of[passed,failed]}}` proof (compile+launch+structured output).

## Invariants
Structured-output-only; proven is earned, never asserted; no harness assumptions outside the
adapter; stdlib-only; env values linted for secret-shaped names.

## Tests
crun's fake-harness corpus ported (M5 exit); stale-binary polarity (edit → must rebuild);
cross-process cache contention at 8-way; pin + proven lifecycle transitions; gtest XML torture
(crashes mid-suite, repeated names, empty suites); guidance lookups with and without snapshot.

## Done
M5 (all of the above vs fixture repos) → M6 (facts handoff on src_compile) → M8 (optional
adapters, each behind an ADR).
