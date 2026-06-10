# engine-facts — `engine/arbiter_engine/facts/`

## Identity
cipher-2 absorbed as the facts namespace. **This module is imported verbatim, not rewritten**
(ADR-0002): the type-driven libclang extraction pipeline, snapshot storage, and the
`search`/`detail` query engine are the bundle's crown jewels and arrive with their conformance
corpus. Only the owner-signed deltas below are permitted; everything else is a red-line change.

## Inherits (kept verbatim, M4 subtree import)
- `initializer/extractor/code/*`: capability probe, streaming type-driven AST traversal
  (ctypes libclang — runtime-located via clang_executable → llvm-config → platform paths),
  bounded per-file process worker pool, worker-local exact relative dedup, facts SQLite reducer,
  relatives external merge, linkage-aware cross-TU `direct_call` resolution.
- `storage/*`: snapshot writer (content-addressed `snapshots/{current,<id>}`), read_index,
  search (tokenized intersection + relation mini-language: one-hop, `dispatches_via:<field>`,
  bounded `callers:`/`callees:` closures, `reachable:A->B` with per-hop conditions), views.
- `mcp/` descriptors: `search`/`detail` request/response schemas **byte-frozen**, budget ladder
  8/32/128KB with staged degradation, anchor tiers, honest `complete`/`total_is_exact`/
  `budget_exhausted` flags.
- `incremental/`: overlay reconcile (dirty-TU re-extraction, header fanout via include graph).
- Failure policy: explicit failure over degradation; `clang_ast_failed` skip + record;
  `clang_ast_partial` on diagnostics-with-usable-AST; never a lightweight-parser fallback.

## Owner-signed deltas (each one is an ADR or design.md §10 phase-3 line item — nothing else)
1. Paths: `.cipher/` → `.arbiter/facts/`; config → `config.yml facts:` section.
2. Chassis: served by engine-core's multi-namespace loop; `_meta` handled outside tool schemas.
3. Reconcile becomes lazy (first fact access, not spawn) and **writer-gated** (player-QUERY
   only); overlay publish takes `overlay.lock`; poll thread + `overlay_ttl_seconds` deleted.
4. Inventory hashing factored to `shared/census` (facts keeps a thin wrapper).
5. cipher's CLI/init/`.mcp.json` writer dropped (go-deploy owns wiring; batch mode via core).

## New capability (M6): extract-cache + build-driven consumption
- `extract-cache/`: per-TU cache keyed `(TU content sha, include-closure content sha,
  allowlist-cleaned semantic flags, toolchain id)` (ADR-0005). The key is **defined as the flags
  the parser actually sees** — the allowlist strips codegen-only flags, so the cache is
  profile-invariant by construction. `facts.key_flags` re-admits configured flags (the
  instrumentation-macro opt-in).
- Consumes the build journal queue (engine-shared pipeline): re-extract changed TUs during the
  build, merge + publish behind the barrier, report `{published, snapshot_id, files, warnings,
  extract_ms, hidden_ms, tail_ms}` to the runs verdict.
- Typed `no_snapshot{hint:"run the gear-up step"}` before first publish.
- Fact-predicate evidence: `{snapshot_id, overlay_id, view_state}` on every adjudicated query.

## Red lines (auto-reject)
Typed AST evidence only — no string-pattern symbol inference; explicit failure over degradation;
`search`/`detail` schemas and budget byte-behavior frozen (transcript-pinned); FACT-only (no
graph projection / concepts / git facts); ctypes runtime libclang loading stays.

## Tests
cipher's 74 test files green unmodified (M4 exit); transcript byte-equality on the conformance
corpus vs cipher-2 responses; extract-cache key property tests (profile switch → 0 re-extracts;
`-DWITH_X` flip → exactly the closure cone; key_flags opt-in restores sensitivity); single-writer
enforcement (executor engine attempting reconcile → typed refusal).

## Done
M4 (absorption + deltas) → M6 (extract-cache + pipeline). Any diff inside the verbatim-kept
subtrees beyond the signed deltas is `needs-human`.
