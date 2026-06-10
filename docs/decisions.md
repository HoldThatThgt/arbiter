# Decisions (ADR log)

Append-only. Each entry is signed by the owner before any code that depends on it merges.
Format: number, date, status, decision, consequences. Spec changes happen ONLY through this file.

---

## ADR-0001 — Product name: Arbiter (2026-06-11, accepted)
"CERT" was the runner-up. Arbiter matches the spine: a referee who decides; the model only
requests adjudication. **Consequences:** binary `arbiter`, engine package `arbiter-engine`,
state dir `.arbiter/`, env prefix `ARBITER_`.

## ADR-0002 — Polyglot with one seam; engine stays Python (2026-06-11, accepted)
Go binary = chess kept nearly verbatim + engineclient + deploy + interpose. Python engine =
cipher-2 absorbed verbatim + crun rebuilt stdlib-only + shared/. cipher is NOT rewritten:
its ctypes runtime-located libclang loading is load-bearing (cgo would pin libclang at build
time or forfeit the static binary), and its hot paths are already native (clang, SQLite,
sha256, child processes). The one per-invocation hot hop — the per-TU compiler shim — lives
in the Go binary (`arbiter cc`). Escape hatch: golden transcripts pin engine behavior, so a
future leaf-by-leaf port is a refactor, not a re-verification. **Consequences:** golden
stdio-transcript contract tests are load-bearing CI forever; AST meta-test enforces the
stdlib-only boundary.

## ADR-0003 — gtest is the first-class harness (2026-06-11, accepted)
The real target is a gtest-guarded C DBMS. The postgres/sqlite sibling checkouts are dummy
benchmarks. pg_regress/TAP/exitcode adapters are optional M8+ work behind owner-signed
result-grammar ADRs; nothing on the critical path depends on them. **Consequences:** the
harness seam exists from day one, but only the gtest adapter ships in M5.

## ADR-0004 — Build-driven indexing; no standalone index ceremony (2026-06-11, accepted)
"Compile done ⇒ index done." The `arbiter cc` shim journals every compiler invocation
(compile-db for free from any build system; the journaled set IS the authoritative TU set)
and enqueues TU extraction overlapped with the build; the `src_compile` run verdict carries
`facts:{published, snapshot_id, extract_ms, hidden_ms, tail_ms}`. `arbiter index` survives
as CI/recovery plumbing only. **Consequences:** cipher's init/rebuild CLI ceremony is not a
user path; gear-up is a templated convention in every opening playbook.

## ADR-0005 — Two caches, two keys (2026-06-11, accepted)
Build cache keys on full flags + profile. Extraction cache keys on (TU content, include-closure
content, allowlist-cleaned semantic flags, toolchain id) — the allowlist strips codegen-only
flags (`-O*`, `-g*`, `-fsanitize=*`, `--coverage`, `-fprofile-*`), so profile switches re-extract
nothing and feature-flag changes re-extract only their include-closure cone. Known blind spot:
compiler-injected instrumentation macros (`__SANITIZE_*`, `__has_feature(*_sanitizer)`);
`/arbiter-intro` runs a whole-token scan and recommends `facts.key_flags` — user-confirmed,
never silently written into committed config. **Consequences:** memoization/cache digests fold
in toolchain hash, goal-spec hash, recipe-book hash; goal memoization ships default-off.

## ADR-0006 — Typed ResultSpec kinds run/fact; deny-self mcp guard (2026-06-11, accepted)
`{kind:"run"}` and `{kind:"fact"}` are the only sanctioned self-evaluation paths (they never
route through `.mcp.json`); any mcp-kind target resolving to `os.Executable()`
(LookPath+Abs+EvalSymlinks) is rejected `reserved_server` outright. mcp-kind gains closed
`expect[]` clauses (`eq|ne|ge|le|exists`, scalars, ≤8) for FOREIGN servers. `[Verify]` blocks
put named predicates in the playbook trust domain; executors invoke them by name.
**Consequences:** adversarial guard test matrix (symlinks, renamed binaries, argv injection)
is a merge gate for go-referee.

## ADR-0007 — Engine distribution: installed package; embedded mode opt-in (2026-06-11, accepted)
Default: pip/uv-installed `arbiter-engine` outside the target repo, pinned by machine-local
gitignored `.arbiter/run/engines.json` with typed staleness errors. Opt-in `--embedded-engine`
(air-gapped): go:embed unpack with sha256 digest verification at EVERY spawn, journaled, plus
Edit/Write deny rules — the adjudication evaluator is never silently model-patchable.

## ADR-0008 — Redaction is per-channel; journal keeps full fidelity (2026-06-11, accepted)
facts.jsonl / runs.jsonl follow cipher/crun strict redaction; `journal.jsonl` (0600, gitignored)
retains chess's full-fidelity forensics because the bypass-cost story depends on fully-logged
evidence. The one deliberate redaction exception, documented here.

## ADR-0009 — Locks and writers (2026-06-11, accepted)
Lock inventory under `.arbiter/locks/`: `match.lock`, `snapshot.lock`, `overlay.lock`,
`state.lock` (+ `BEGIN IMMEDIATE` for proven-lifecycle RMW), `build/<sha8(workdir)>.lock`.
Facts single-writer rule: only the player's QUERY engine reconciles/publishes overlays; all
engines read base + latest published overlay; fact-predicate evidence records
`{snapshot_id, overlay_id, view_state}`. Claim level: "DB-safe and build-serialized" until
8-way contention tests pass. `arbiter init` refuses network filesystems (typed error).

---

*Template for new entries:*

## ADR-NNNN — <title> (<date>, proposed|accepted|rejected)
<decision in 2-6 sentences. consequences as "**Consequences:** ...">
