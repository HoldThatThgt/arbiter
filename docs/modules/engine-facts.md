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

## Toolchain — the two-toolchain isolation contract (inherited from cipher-2, restated)
- **Extraction toolchain:** facts extraction requires an executable Clang **and** a
  same-toolchain libclang, both passing the typed AST capability probe. Officially supported:
  **LLVM Clang ≥ 16, Apple Clang ≥ 15**. libclang is located at runtime (clang_executable →
  llvm-config → platform paths; explicit `libclang_library` is a last-resort escape hatch) and
  a clang/libclang major-version mismatch is a typed failure (`libclang_version_mismatch`).
- **Build toolchain: independent and untouched.** The target repo builds with whatever its
  build system needs — gcc/g++ of any vintage is the *normal* case for the target DBMS class.
  The AST path never requires GCC; arbiter never replaces, substitutes, or version-gates the
  repo's compiler (the `arbiter cc` shim execs it bit-exact — go-interpose.md).
- **The seam between them:** journaled/compile-db per-file argv from the build compiler is
  **allowlist-cleaned before libclang ever sees it** — response files expanded, plugin/output/
  link/non-allowlisted (incl. gcc-only) arguments dropped, relative include/sysroot paths
  normalized against the entry `directory` — so a gcc-built TU set parses under extraction's
  own Clang. Flag cleaning at this seam serves *parseability*; ADR-0005's codegen-flag
  stripping serves *cache keying* — same mechanism, two distinct obligations.
- Clang/libclang unavailable, capability probe failure, or version mismatch **block facts
  extraction explicitly** (typed errors) and must never degrade the build, the refereed loop's
  shell/mcp predicates, or the bundled diagnostics — facts become unavailable, never faked.

## Owner-signed deltas (each one is an ADR or design.md §10 phase-3 line item — nothing else)
1. Paths: `.cipher/` → `.arbiter/facts/`; config → `config.yml facts:` section.
2. Chassis: served by engine-core's multi-namespace loop; `_meta` handled outside tool schemas.
3. Reconcile becomes lazy (first fact access, not spawn) and **writer-gated** (player-QUERY
   only); overlay publish takes `overlay.lock`. The poll thread and `overlay_ttl_seconds` are
   **retained** as the owner-mandated **live background index** (ADR-0018), not deleted: the
   session-resident daemon poll thread keeps the published overlay warm between reconciles, and
   `overlay_ttl_seconds` is a live, validated overlay-GC knob (`0` = GC disabled) — revived from
   cipher-2's never-started skeleton.
4. Inventory hashing factored to `shared/census` (facts keeps a thin wrapper).
5. cipher's CLI/init/`.mcp.json` writer dropped (go-deploy owns wiring; batch mode via core).

## Build-driven consumption (M4 absorption)
- The absorbed cipher-2 extractor publishes **content-addressed snapshots**:
  `snapshots/<sha256-…>/` holds `facts.jsonl.gz`, `relatives.jsonl.gz`,
  `source_inventory.jsonl.gz`, a SQLite `read_index.sqlite`, `manifest.json`, and `stats.json`,
  with a `snapshots/current` pointer file naming the live id. The snapshot id is the sha256 of the
  fact content; the **profile is part of every source id** (`source:hash(profile:rel_path)`), so a
  sanitizer profile (e.g. `asan`) re-extracts under its own snapshot rather than reusing the plain
  build's — there is no separate per-TU extract-cache. `facts.key_flags` is the
  instrumentation-macro opt-in (re-admits configured flags into the parser's view).
- Build-driven seam (engine-shared pipeline): tail the compile journal during `src_compile` →
  emit the compile-db → `CodeFactExtractor(root, config).collect(None, profile)` over exactly the
  compiled TU set → `FileFactStore.replace_snapshot(...)`; report `{published, snapshot_id, files,
  warnings, extract_ms, hidden_ms, tail_ms}` to the runs verdict. A miss-marked or non-green build
  fails closed (no publish); a missing/incapable toolchain degrades to a typed not-published
  signal, never a crash.
- Typed `no_snapshot{hint:"run the gear-up step"}` before first publish.
- Fact-predicate evidence: `{snapshot_id, overlay_id, view_state}` on every adjudicated query.

## Red lines (auto-reject)
Typed AST evidence only — no string-pattern symbol inference; explicit failure over degradation;
`search`/`detail` schemas and budget byte-behavior frozen (transcript-pinned); FACT-only (no
graph projection / concepts / git facts); ctypes runtime libclang loading stays.

## Tests
cipher's 74 test files green unmodified (M4 exit, ported with the absorption); the recorded
facts-conformance corpus (ADR-0013) replayed byte-exact against the engine — the in-tree
cipher-2 reference is retired, and new corpus scenarios are cross-checked against upstream
cipher-2 out-of-tree before their lines become the pin; build-driven seam tests (a green build
round-trips journal → compile-db → snapshot; a sanitizer profile publishes its own
content-addressed snapshot; miss-marker / non-green / incapable-toolchain all fail closed);
single-writer enforcement (executor engine attempting reconcile → typed refusal).

## Done
M4 (absorption + deltas) delivered the build-driven pipeline seam directly — the absorbed
extractor publishes content-addressed snapshots, so no separate per-TU extract-cache exists. Any
diff inside the verbatim-kept subtrees beyond the signed deltas is `needs-human`.
