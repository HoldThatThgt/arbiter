# Proposal: M4 — absorb the cipher-2 facts engine (real extraction, search/detail, incremental)

Status: **landed** — owner decisions recorded 2026-06-15 (see §8); all phases implemented and
`make test`-green on `m4/facts-absorption` (Phase 1.5 seam + extractor/initializer acceptance +
Phase 2 incremental engine, live overlay reconcile/merge, and the background-index daemon). The
prose below is the as-built plan; ADR-0018 records completion.
Relates to: ADR-0002 (polyglot, cipher-2 absorbed verbatim), ADR-0004 (build-driven indexing),
ADR-0005 (two caches/keys), ADR-0013 (in-tree cipher-2 retired; recorded corpus is the pin),
ADR-0015. Supersedes the "reserved" disposition of `facts.extractor`/`facts.incremental`
([[facts-config-keys-disposition]]).

---

## 1. Problem

The facts subsystem is wired end-to-end but **hollow**:

- The extractor is a placeholder — `_default_extractor` ([shared/pipeline.py:229](../../engine/arbiter_engine/shared/pipeline.py)) returns `{source, warnings:[]}` and never parses an AST.
- A published snapshot is a **file-list manifest** (`_publish_snapshot`), not a fact corpus.
- `search` hard-codes `result_count: 0, results: []` and `detail` always returns `not_found`
  ([rpc/__init__.py `_facts_search_tool`/`_facts_detail_tool`](../../engine/arbiter_engine/rpc/__init__.py)).

Consequence on the **adjudication path**: any `fact`-kind predicate that needs a real hit —
`expect: {min_results: ≥1}`, `reachable`, `complete` — can **never pass**. Only `max_results: 0`
and the run-predicate `facts: {published: true}` clause are meaningful today. The player's
"orient fact-first" step returns nothing. The incremental overlay-reconcile subsystem does not
exist. This is a correctness hole, not just a missing feature.

## 2. Strategy

Absorb cipher-2's proven facts engine per ADR-0002/0013 — a **near-verbatim import adapted at
the seams**, pinned by the conformance corpus + golden transcripts. cipher-2 source lives at
`~/Project/cipher-2/src/cipher2/`. It is **stdlib + ctypes-libclang only** (no pip deps), so it
passes arbiter's `test_stdlib_imports` gate unchanged.

Two de-risking facts already hold in arbiter:
1. The **wire contract is already frozen** to cipher-2's exact shape — search/detail JSON schemas
   ([facts/descriptors.py](../../engine/arbiter_engine/facts/descriptors.py)) and the Go referee's
   field reads (`internal/verify`) already expect `result_count`/`total`/`complete`/`reachable`/
   `base_snapshot_id`. We are filling a prepared socket, not redesigning it.
2. Arbiter already owns the **harder halves of incremental** — `view.py` (writer-gate via real
   `flock`, atomic overlay-state publish, content-addressed overlay id) and `shared/census.py`
   (mtime+sha dirty detection). (The placeholder-era `facts/extract_cache.py` was removed in
   Phase 1.5b — cipher-2's absorbed extractor brings its own dirty re-extraction
   (`extractor.extract_dirty_sources`), so the old per-TU semantic-key cache is superseded.)

## 3. The integration contract (frozen — the port conforms to this, not vice-versa)

### search
- **Input** ([descriptors.py:65-73](../../engine/arbiter_engine/facts/descriptors.py)): `query`
  (string, required), `limit` (int 1..50, default 20), `additionalProperties:false`. `depth:N` is
  parsed *out of the query string*, never an input arg.
- **Required output keys**: `view_state, base_snapshot_id, overlay_id, stale_source_count,
  pending_task_count, status (ok|too_broad|needs_refinement), query_kind (empty|terms|relation|
  relation_transitive|relation_reachable), query, limit, result_count, truncated, results[]`
  (each result carries `object_id`). Optional per query kind: `relation, anchor, total, message,
  available_filters, examples, top_by_salience, anchor_candidates, matched_endpoint_count,
  complete, budget_exhausted, budget_exhausted_kind, total_is_exact, reachable, path,
  depth_requested, depth_used, depth_max`.

### detail
- **Input**: `fact_id` (string, required — a search `object_id`), `budget (small|normal|large)`.
- **Required output**: `view_state` group + `fact, payload, payload_truncated, source_context
  (nullable), relative_preview (callers/callees/field buckets)`. Missing id → JSON-RPC `not_found`
  error (not a 200) with the exact existing message text.

### Go referee field mapping (load-bearing — fact predicates break if mis-emitted)
`internal/verify` (`factEvidenceFromStructured` / `CompareFact`) reads `structuredContent`:

| predicate clause | op | actual field | source key (fallback) |
|---|---|---|---|
| `min_results` | ge | ResultCount | `result_count` |
| `max_results` | le | ResultCount | `result_count` |
| `complete` | eq | Complete | `complete` (else `!truncated`) |
| `reachable` | eq | Reachable | `reachable` (else `false`) |
| `total_at_least` | ge | TotalResults | `total` (else `result_count`) |

Snapshot evidence reads **`base_snapshot_id`** (not `snapshot_id`). Emit native JSON ints/bools.
The port MUST emit `total` and `truncated`/`complete` honestly for relation queries, and
`reachable` for `reachable:` queries.

### Fact record schema (the new fact-evidence shape)
`FactRecord{object_id, object_name, object_description, object_source ("path:line"),
object_profile, object_caller, object_callee, payload{fact_kind, canonical_source, line,
ordinal, linkage, name, …kind-specific}}` (≤4KB payload). Relation edges are **separate**
`FactRelative` rows ({relative_id, from_fact_id, to_fact_id, relation_kind ∈ {include, defines,
declares, has_field, direct_call, assigned_to, dispatches_via, field_read, field_write},
condition?, payload}, ≤2KB). `fact_kind ∈ {code_file, function, global, type, field, macro,
function_pointer_slot}`.

## 4. What exists vs what to port

| Component | Status in arbiter | Action |
|---|---|---|
| search/detail JSON schemas | ✅ frozen, correct | keep |
| Go fact-evidence mapping | ✅ correct | keep; add populated-snapshot tests |
| `view.py` writer-gate / overlay-state / locks | ✅ present, strong | keep as Phase-2 skeleton |
| `census` dirty detection | ✅ present | reuse in Phase 2 |
| ~~`extract_cache` semantic key~~ | ❌ removed (Phase 1.5b) | superseded by cipher-2 `extract_dirty_sources` |
| **AST extractor** (libclang) | ❌ placeholder | **port** `extractor/code/*` (~8.3k LOC) |
| **fact store + SQLite read-index** | ❌ manifest only | **port** `storage/*` (~7.6k LOC focused) |
| **search/detail query engine** | ❌ hard-coded empty | **port** `storage/search.py`+`read_index.py`, rewire handlers |
| **incremental overlay content + merge** | ❌ state pointer only | **port** `incremental/*` (~0.8k) + `views.py` merge |

## 5. Phase 1 — real facts + working search/detail

Forced first (incremental overlays merge over a base store).

- **1.1 Store models/constants** → new `engine/arbiter_engine/facts/store/`: port cipher-2
  `storage/{constants,models,serialization,utils}.py`. Inline `cipher2.common.JSONValue`; stub
  `cipher2.tools.log` (no-op). Carry payload caps (4KB/2KB/1KB), `RELATION_KINDS`,
  `RELATION_SEARCH_DEFINITIONS`, `FACT_KIND_SEARCH_RANKS`, `SCHEMA_VERSION`.
- **1.2 Extractor** → new `engine/arbiter_engine/facts/extractor/`: port cipher-2
  `initializer/extractor/code/{ast_backend,mapper,mapper_utils,toolchain,compile_db,direct_calls,
  constants,models,streaming}.py`. Replace `CipherConfig` with a 6-field shim
  (`compile_database_path, clang_executable, libclang_library_path, gcc_executable, clang_args,
  extractor_worker_count`) sourced from the recipe/compile-db + `FactsConfig`. **Worker count =
  `facts.index_on_build.pool`** (already wired as the build-tail cap — unify the two). Repoint the
  map-reduce staging dir from `.cipher/run/...` to `.arbiter/facts/run/`.
- **1.3 Snapshot store + read-index** → `facts/store/`: port `storage/{snapshot_writer,
  snapshot_reader,read_index,views,store}.py`. Content-addressed
  `.arbiter/facts/snapshots/<id>/{facts.jsonl.gz, relatives.jsonl.gz, source_inventory.jsonl.gz,
  read_index.sqlite, manifest.json, stats.json}` + atomic `current` pointer. Keep `manifest.json`
  carrying `snapshot_id` (read by `view._base_snapshot_id`).
- **1.4 Pipeline seam** ([shared/pipeline.py](../../engine/arbiter_engine/shared/pipeline.py)):
  `_default_extractor` → a real extractor returning `FactRecord`/`FactRelative`;
  `_extract_pending` retains records (not `{source, failed}`); `_publish_snapshot` writes the store
  under the `SNAPSHOT` lock; **preserve the no-prune merge** (incremental builds see a subset of
  units). Honor the `facts.extractor` selector (default `"clang"`); `key_flags`/`pool` already
  thread through.
- **1.5 Query wiring** ([rpc/__init__.py](../../engine/arbiter_engine/rpc/__init__.py)):
  `_facts_search_tool`/`_facts_detail_tool` query the store resolved via
  `view._base_snapshot_id`; emit the frozen schema; populate `result_count/total/complete/
  truncated/reachable`. Port the full grammar (plain-term AND, `callers:`/`callees:` closures,
  `reachable:A->B`, `dispatches_via:`, `readers`/`writers`/`accessors`, `depth:N`).
- **1.6 Degradation**: no capable libclang → no facts → **empty snapshot** (capability-probed,
  typed `clang_ast_failed`/gear-up warning). This preserves today's empty-corpus behavior exactly
  (see pins).

## 6. Phase 2 — `incremental.*`

- **2.1 Dirty plan**: census `changed_sources` feeds cipher-2's own dirty re-extraction
  (`extractor.extract_dirty_sources` + its header/include-closure fanout). The placeholder-era
  arbiter `extract_cache` was removed in Phase 1.5b, so Phase 2 uses the absorbed extractor's
  incremental path rather than reimplementing a semantic-key cache.
- **2.2 Overlay build**: re-extract the dirty set via the Phase-1 extractor; write an overlay
  patchset (`facts.upsert/tombstone.jsonl`, `relatives.upsert/tombstone.jsonl` + manifest) under
  `.arbiter/facts/overlay/<id>/`; endpoint-closed validation; content-addressed overlay id (arbiter
  already derives `overlay:<digest>` — keep it over cipher-2's random UUID).
- **2.3 Overlay merge at query time**: port `storage/views.py` merge so `FactView.search/get_fact/
  relatives` union base (SQLite) + upserts and subtract tombstones (the Python query twin already
  ships in `search.py`). Wire into `view.access` (writer→reconcile, reader→read_published).
- **2.4 Background coordinator (owner-required)**: `facts.incremental` flips from reserved →
  **live**, driving an **automatic background index**. Port cipher-2's `IncrementalCoordinator`
  poll loop as a session-resident daemon thread hosted **inside the player's QUERY engine** (the
  facts single-writer, ADR-0009): started on first writer access, stopped on stdin EOF / engine
  shutdown (torture-tested for no orphan, like the seat children). Make the config knobs **live**:
  `poll_interval_ms`, `debounce_ms`, `worker_count` (parallel dirty re-extraction — = the unified
  `pool`), `overlay_ttl_seconds` (overlay GC — cipher-2 left this a no-op; we implement it),
  `max_dirty_files`. The thread shares the `OVERLAY` flock with synchronous `reconcile` and never
  races the base publisher (overlays live in a disjoint dir). The referee's `arbiter/refresh` still
  forces a synchronous reconcile before fact predicates, so **adjudication is never stale**; the
  background thread only keeps the index warm between refreshes. Update
  [[facts-config-keys-disposition]] and the user-guide reserved-key note.

## 7. Pins & regeneration (must-not-break)

1. **Conformance corpus** (`test_facts_conformance_corpus.py`, fixture
   `fixtures/facts_conformance/empty_corpus.jsonl`): the **5 empty-snapshot lines are immutable**.
   With no `.arbiter/facts` snapshot, search MUST still return `result_count:0, results:[],
   status:"ok", view_state:"base", base_snapshot_id:null` and detail the exact `not_found` text.
   Populated-snapshot scenarios are **append-only** (bump `EXPECTED_CASES`) and must be
   cross-checked against upstream cipher-2 **out-of-tree** before becoming the pin (ADR-0013).
2. **Golden transcripts** (`make transcripts` → `write_transcripts.py`, asserted by
   `test_transcript_corpus.py`): populated results make `base_snapshot_id` + record ids
   non-deterministic. Either extend `_volatile_paths` masking to cover them, **or** seed a
   deterministic snapshot in `record()`. Keep the existing tool/budget/limit coverage set.
3. **stdlib-only** (`test_stdlib_imports.py` + `import_policy.py`): ctypes/sqlite3/hashlib pass; a
   pip `clang` dep fails. Any guarded-optional import must be `try/except` + registered in
   `OPTIONAL_EXTRA_IMPORTS`.
4. **Go + verify tests**: add populated-snapshot fact-predicate tests (`min_results≥1`, `reachable`,
   `total_at_least`) so the adjudication path is exercised, not just the empty path.
5. **cipher-2 test acceptance** (owner-required): cipher-2's own facts tests are migrated as EXTRA
   tests into `engine/tests/c2/` and **must pass against the port** — the full plan (24
   `test_*.py` files / 233 tests in c2 across both phases — Phase 1 (non-incremental): 22 / 221;
   Phase 2 (incremental): 2 / 12; the 3rd Phase-2 file `test_config_incremental.py` is new-native
   at `engine/tests/` top-level, outside c2 — plus 10 partial, 33 excluded) is
   [m4-test-migration-map.md](m4-test-migration-map.md).
   Each phase migrates its mapped tests once its code lands, keeping `make test-py` green.

## 8. Decisions — resolved by the owner (2026-06-15)

1. **Package layout** — nested `facts/store/` + `facts/extractor/`. ✅
2. **Worker count** — unified: one knob, `facts.index_on_build.pool`, drives both the build-tail
   extraction and incremental dirty re-extraction. ✅
3. **Incremental** — **automatic background index is REQUIRED** (not on-demand only; *changed from
   the proposed default*). Keep cipher-2's poll loop and make every knob live
   (`poll_interval_ms`/`debounce_ms`/`worker_count`/`overlay_ttl_seconds`/`max_dirty_files`),
   including overlay TTL/GC which cipher-2 never implemented. Host the daemon thread in the player
   QUERY engine; see §6.2.4. ✅
4. **Query grammar** — full grammar in Phase 1. ✅
5. **ADR** — record M4 completion + `facts.incremental` going live as **ADR-0018** when Phase 2
   lands. ✅

## 9. Risks & mitigations

- **libclang availability/version** — capability-probed already; absent ⇒ empty snapshot (typed
  failure on gear-up), builds/matches unaffected. Documented requirement (LLVM Clang ≥16).
- **Determinism for transcripts** — mask or seed (item 7.2).
- **Schema/payload caps & cross-TU `direct_call` resolution** — port cipher-2's validation +
  fixtures (`test_code_extractor_fixtures.py`) so behavior is pinned.
- **LOC volume / review burden** — phased; each sub-step ends with `make test` + the pin suite
  green; no sub-step merges red.
- **Snapshot store size on disk** — gzip-1 JSONL + SQLite, gitignored under `.arbiter/facts/`.

## 10. Definition of done

- **Phase 1**: a gear-up over a real C repo publishes a populated snapshot; `search("<symbol>")`
  returns hits with `result_count/total`; `callers:`/`reachable:` work; `detail(object_id)` returns
  the record + source context; `fact`-predicates with `min_results≥1`/`reachable` pass; empty-corpus
  conformance lines byte-identical; transcripts regenerated; stdlib-only + full suite green.
- **Phase 2**: with `facts.incremental` enabled, editing a source (or a header, via fanout)
  surfaces updated facts through an overlay **automatically within ~`poll_interval_ms`** (no
  explicit refresh needed); `arbiter/refresh` also forces it synchronously; the background thread
  starts with the player QUERY engine and exits cleanly on EOF (no orphan); overlays past
  `overlay_ttl_seconds` are GC'd; base+overlay results match the pure-base results after a full
  rebuild; suite green.

## 11. Out of scope / deferred

cipher-2's `doc`/`git` extractors (only `code` is implemented upstream); pg_regress/TAP harnesses;
multi-language relation vocabularies. (Background poll-loop + overlay TTL/GC are now **in scope** per
§6.2.4, decision #3.) The `source_inventory` machinery is ported in full — Phase 2 needs
`included_by` for header fanout and `sha256/mtime_ns` for dirty detection.
