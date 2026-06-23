# M4 Test Migration Map — cipher-2 → arbiter facts acceptance spec

Status: **accepted** — companion to [m4-facts-absorption.md](m4-facts-absorption.md). Defines the
acceptance set: arbiter's ported facts engine must pass cipher-2's own tests, migrated as **EXTRA**
tests beside arbiter's native suite. A migrated file lands **only after its target code is ported**
so `make test-py` stays green at every step.

The store package is already ported (`engine/arbiter_engine/facts/store/`); the `_common.py` no-op
log shim and the `_StorageCompatModule` setattr fan-out (needed by `mock.patch.object`) are in place;
the wire schema is frozen in `facts/descriptors.py`; `facts/view.py` owns the flock writer-gate. The
extractor package (`facts/extractor/`) does **not** exist yet.

> Convention note: migrated tests live in `engine/tests/c2/` (a unittest-discovered subpackage),
> keeping the upstream filenames. The JSON test libclang backend installer is re-homed into that
> package's `__init__.py` (the unittest equivalent of cipher-2's `tests/__init__.py`).

---

## 1. Mechanism & convention

### 1.1 Where migrated tests live
`make test-py` → `PYTHONPATH=engine python -m unittest discover -s engine/tests` recurses into
subpackages with an `__init__.py`. Migrated tests live in **`engine/tests/c2/`** keeping their
cipher-2 names; the package `__init__.py` is also the home for the JSON libclang backend installer
(§1.3). The stdlib-import gate scans `arbiter_engine/` only, so migrated tests may import
`unittest`/`tempfile`/`unittest.mock` freely — but the **ported engine code** they exercise stays
stdlib-only.

### 1.2 Import rewrite (mechanical)
| cipher-2 import | arbiter target |
|---|---|
| `import cipher2.storage as storage_module` | `import arbiter_engine.facts.store as storage_module` |
| `from cipher2.storage import …` | `from arbiter_engine.facts.store import …` |
| `from cipher2.storage.recovery import force_unlock` | `from arbiter_engine.facts.store.recovery import force_unlock` |
| `from cipher2.initializer.extractor.code import …` | `from arbiter_engine.facts.extractor.code import …` |
| `from cipher2.mcp import open_mcp_server` | **no analog** — drive `arbiter_engine.rpc` handlers (§1.5) |
| `from cipher2.config import load_config, write_default_config` | **no analog** — 6-field extractor-config shim builder |
| `from cipher2.tools.log import open_log` | `arbiter_engine.facts.store._common.open_log` (no-op) — drop log assertions (except extractor/incremental, which get a real log) |
| `from cipher2.incremental import …` | `arbiter_engine.facts.incremental.…` (Phase 2) |
| `from tests.toolchain_helpers import …` | `from c2.toolchain_helpers import …` (re-homed into the test subpackage) |

The `store/__init__.py` `_StorageCompatModule` makes `mock.patch.object(storage_module, …)` fan out
to submodule globals — already wired, so the file-store/relative-store mocks port unchanged.

### 1.3 JSON libclang backend (hermetic extractor tests, no real clang)
cipher-2's `tests/__init__.py` calls `_install_json_test_libclang_backend()` at import so extractor
tests run against a JSON-subprocess AST oracle. Reproduce in arbiter as test infrastructure: Phase 1.5
ports `_install_json_test_libclang_backend`/`_JsonSubprocessTestBackend`/`_clear_test_libclang_backend`
+ the `_TEST_AST_BACKEND_FACTORY` hook under `arbiter_engine.facts.extractor.code`, and the install
side-effect goes in `engine/tests/c2/__init__.py`. Keep the probe AST node identifiers
(`PROBE_FUNCTION_NAME`) byte-aligned.

### 1.4 Shared helpers & fixtures
`toolchain_helpers.py` re-homes to `engine/tests/c2/toolchain_helpers.py` (its `write_default_config`
tail → the 6-field extractor-config shim builder). No external on-disk fixtures: every store/extractor/mcp
test builds records inline into a `tempfile.TemporaryDirectory`.

### 1.5 No `open_mcp_server` — drive the rpc chassis
cipher-2 uses `open_mcp_server(target).search(...).structured_content`; arbiter binds tools to
`Path.cwd()` + a `Context(role, seat)` from `ARBITER_ENGINE_ROLE`/`ARBITER_ENGINE_SEAT` (the harness in
`test_facts_reconcile.py`/`test_facts_conformance_corpus.py`). Per file: (a) a thin `open_facts_server(repo)`
shim exposing `.search()/.detail()/.call_tool()` over `_facts_search_tool`/`_facts_detail_tool` for the heavy
grammar specs, or (b) dict-shape rewrite over `rpc.serve`. Error taxonomy remaps to `RPCError(kind=…)`
(`unknown_tool`→`-32601 tool_not_found`, extra-arg→`-32602 invalid_args`, etc.). The detail `not_found`
text is already byte-frozen in arbiter — keep.

### 1.6 `.cipher/` → `.arbiter/facts/` paths
The store root already moved (`store.py`, `recovery.py` fixed). Tests that hard-code on-disk paths rewrite
the literal `.cipher` segment → `.arbiter/facts` (snapshots/`<id>`, `run/storage.lock`, `log`,
`snapshots/current`, extractor staging `run/initializer-mapreduce`). `store/recovery.py:11` is fixed.

### 1.7 Wiring per phase (stay green)
Migrate a file only when its gating phase's code is ported: **1.2** pure-model files (now), **1.3**
store/read-index/search files (now — store fully ported), **1.5** mcp/query + extractor end-to-end, **2**
incremental. Each step ends with `make test` green.

---

## 2. Per-phase acceptance (included + partial)

### Phase 1.2 — store leaf (models/serialization) + extractor
- `test_storage_fact_record.py` (6) — **migrated, green.** Pins `SCHEMA_VERSION==5`, `invalid_fact`/`payload_too_large`, 4KB cap, frozen-slots.
- `test_storage_relative_record.py` (7) — landable now. `RELATION_KINDS`, `invalid_relative`/`invalid_relation_kind`/`invalid_condition`/`condition_too_large`(1KB)/`payload_too_large`(2KB), confidence∈[0,1].
- `test_code_extractor_fixtures.py` (54), `test_code_extractor_parallel.py` (5), `test_initializer_toolchain.py` (33 total, split across this phase + Phase 1.5 band B), `toolchain_helpers.py` (infra), `tests/__init__.py` (infra) — gate on `facts/extractor/` (Phase 1.5). Extractor keeps a **real** log (not the store no-op).

### Phase 1.3 — snapshot store + read-index + locks (store fully ported → landable now)
`test_storage_source_inventory.py` (2), `test_storage_view_model.py` (5), `test_storage_relative_no_compat.py` (2),
`test_storage_file_store.py` (15), `test_storage_relative_store.py` (17, uses `search.py` — ported),
`test_storage_corruption.py` (12), `test_storage_path_safety.py` (5). Adaptation: import + `.cipher`→`.arbiter/facts`
rewrites; codes are the spec (`snapshot_corrupt`/`manifest_mismatch`/`unsupported_schema_version`/`missing_snapshot`/
`stats_mismatch`/`path_escape`/`lock_busy`). The store keeps cipher-2's mkdir-lock surface (`store_events`/`recovery`
ported verbatim), so the lock tests port cleanly.

### Phase 1.5 — query wiring (search/detail) + extractor end-to-end
Included: `test_mcp_search_detail.py` (7), `test_mcp_relation_search.py` (16, the relation-grammar spec),
`test_mcp_performance.py` (2, recalibrated smoke — guards bounded fetch). Partial (drop log sub-assertions / remap
errors / new-native protocol shape): `test_mcp_response_budget` (3), `test_mcp_tool_models` (5),
`test_mcp_relations` (9; 2 skipped — endpoint-closure — so 7 execute), `test_mcp_stdio_protocol` (2), `test_initializer_api` (6),
`test_initializer_compile_database` (4), `test_initializer_path_safety` (4), `test_initializer_toolchain` band B (the remainder of its 33).
Port the query layer (`SearchResponse`/`DetailResponse`/`RelationPreview`/budget ladder/`_bounded_payload`/
`_source_context`) behind the rpc handlers; replace the stub `_query_kind` with `parse_relation_search_query`.

### Phase 2 — incremental.*
`test_incremental_overlay_view.py` (9, include), `test_incremental_mcp_view_state.py` (3, new-native rpc tests)
— these two are the only Phase-2 files **inside `engine/tests/c2/`**.
`test_config_incremental.py` (3, new-native arbiter config) lives at **`engine/tests/` (top-level, NOT c2)**, so it
is outside the 24-file/233-test c2 acceptance set — plus a fresh facts-config knob test. The incremental
coordinator needs a **real** jsonl log. `worker_count`→`facts.index_on_build.pool` (decision #2); knobs live
(decision #3). Keep content-addressed `overlay:<digest>`.

---

## 3. Exclusions (not ported; new-native successor noted)

- **Observability/dashboard** (`test_storage_observability`, `test_views_*` = `build_overview` dashboard, `test_mcp_observability`, `test_initializer_observability`) — depend on the real `cipher2.tools.log` sink + `cipher2.tools.views`; StorageStats covered by `test_storage_view_model`. *new-native if arbiter grows telemetry.*
- **Log subsystem** (`test_log_*`) — `cipher2.tools.log` not ported (`_common.open_log` is a no-op). *new-native against `arbiter_engine/log/` if wanted.*
- **Config subsystem** (`test_config_defaults`/`file`/`observability`/`path_safety`/`coverage_matrix`) — `cipher2.config` replaced by arbiter's stricter parser + the 6-field shim; the live-incremental-knob behavior survives as a new-native facts-config test.
- **CLI subsystem** (`test_cli_*`) — replaced by arbiter's Go CLI + deploy; acceptance belongs to the Go tests.
- **Coverage-matrix meta-tests** (`test_*_coverage_matrix`) — re-author one fresh arbiter-native matrix once the layout freezes.
- **Benchmarks** (`test_retrieval_*`) — depend on unported `benchmarks.retrieval` + `cipher2.mcp`. (Their record usage confirms `object_profile`/`evidence_source`/`confidence`/`payload` stay optional on the ported dataclasses.)

---

## 4. Totals & risks

**Totals:** 24 included `test_*.py` files (233 tests) in `engine/tests/c2/` across both phases —
Phase 1 (non-incremental): 22 files / 221 tests; Phase 2 (incremental): 2 files / 12 tests
(`test_config_incremental.py` is a 3rd Phase-2 file but lives at `engine/tests/` top-level, outside
c2, so it is not in this count). 10 partial, 33 excluded. recordCount 61.

**Risks**
1. **Store lock-surface drift** — mitigated: cipher-2's mkdir-lock (`store_events`/`recovery`) was ported verbatim, so `test_storage_path_safety` ports cleanly; `recovery.py:11` `.cipher` bug fixed.
2. **No `open_mcp_server` analog** — thin `open_facts_server` shim (§1.5) for heavy typed assertions; dict-shape rewrite otherwise; errors → `RPCError(kind=…)`.
3. **Config-schema divergence** — all `cipher2.config` tests excluded; live incremental knobs re-homed new-native (`worker_count`→`pool`, section→`facts.incremental`).
4. **No structured log** — drop `read_events`/`summarize` sub-assertions; **exception:** extractor + incremental coordinator need a real jsonl log (not the store no-op).
5. **Perf tests recalibrate, don't delete** — `test_mcp_performance` kept as a recalibrated bounded-fetch smoke; `test_initializer_performance` peak-formula unit excluded (helper unported), its streaming-memory invariant covered by `test_initializer_api`.
6. **libclang** — extractor tests hermetic on the JSON backend; 3 real-toolchain band-C tests self-skip via `shutil.which("clang")`.
