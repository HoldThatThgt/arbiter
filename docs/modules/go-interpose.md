# go-interpose — `internal/interpose` (`arbiter cc`)

## Identity
The per-TU compiler shim that makes build-driven indexing possible (ADR-0004). Exec'd once per
compiler invocation — thousands of times per full build — so it lives in the ms-startup Go
binary, never Python (interpreter startup × N TUs is a minute-plus tax at DBMS scale; the
ccache lesson).

## Public surface
```
arbiter cc -- <real-compiler> [original args...]
```
Injected by the runs engine into `src_compile` stages as `CC="arbiter cc -- <real-cc>"` /
`CXX=...` / `CMAKE_C_COMPILER_LAUNCHER` (form recorded in the recipe by `/arbiter-intro`).
Never invoked by users or models directly.

## Design
1. **Classify** the invocation: a compile step (has `-c` / produces an object from a recognized
   C/C++ source) vs link/preprocess/other. Only compile steps are journaled; everything execs
   through regardless. Response files (`@file`) are expanded for classification and journaling;
   the original argv is what gets exec'd.
2. **Journal**: append `{argv, cwd, src, out, ts}` as one line to the build-local journal
   (`.arbiter/facts/run/compile-journal.<build_id>.jsonl`). Appends are O_APPEND single-write
   sized under PIPE_BUF or guarded by a journal flock — safe under `make -jN`. The journaled set
   is the authoritative TU set for this build (strictly stronger than a static
   compile_commands.json: it is what was *actually built*).
3. **Enqueue**: the journal IS the queue; the engine's pipeline consumer (engine-shared) tails it.
   The shim never blocks on the engine.
4. **Exec-through**: `execve` the real compiler with untouched argv. **Fail-open for the build**:
   any internal shim error → exec the compiler anyway and append a `{miss}` record (or, if even
   journaling fails, write a marker file). A shim defect can never break compilation; a shim miss
   surfaces later as `facts:{published:false|warnings}` failing the gear-up predicate closed.
5. **Stacking**: tolerate ccache/distcc already present in `CC` (we wrap the whole string after
   `--`); detect self-wrap (`arbiter cc` wrapping `arbiter cc`) and collapse.

## Invariants
Exit code, stdout, stderr of the real compiler pass through bit-exact. No network, no engine
dependency, no Python. Startup budget: ≤3ms p95 on darwin/linux dev machines (measured in CI by
a 1000-invocation benchmark against `true`).

**Build-compiler-agnostic (the two-toolchain contract, engine-facts.md):** `<real-compiler>`
is the repo's own toolchain — gcc/g++ of any version, clang, icc, a cross compiler — and the
shim only journals and execs it; it never substitutes another compiler, injects flags, or
imposes a version requirement. Extraction's own Clang/libclang requirement (LLVM ≥ 16) is a
*downstream* concern of the journal consumer: a host without it loses facts publication
(`facts:{published:false}` fails the gear-up predicate closed) while the build itself remains
untouched and green.

## Tests (the adversarial matrix is a merge gate — lands BEFORE the happy path)
Response files; multi-arch flag soup; `-MD/-MF` depfiles passthrough; stacked ccache; self-wrap
collapse; `make -j16` journal-integrity (no torn lines, no lost records vs a reference strace
count); interrupted build (partial journal must be consumable + idempotently re-runnable);
compiler-not-found (exit code faithfully propagated); space/quote-hostile paths; symlinked
compilers.

## Done
M6, first PR of the milestone. Any change to classification or journal format updates
engine-shared's consumer + transcripts in the same PR.
