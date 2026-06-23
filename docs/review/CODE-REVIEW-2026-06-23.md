# Arbiter Code Review — `main` @ 3c52a17 (2026-06-23)

**Scope:** full codebase — ~9.7k LOC Go (`cmd/` + `internal/`) and ~22k LOC Python engine (`engine/arbiter_engine/`), plus ~17k LOC Python tests and the `docs/` tree (20 ADRs, module specs, proposals, `FORMAT.md`).
**Goal:** correctness **bugs** + **doc/code mismatches**, with adversarial focus on the referee's trust boundaries (frozen-test immutability, recipe pinning, deny-self, single-writer facts, verdict soundness).
**Method:** 21 parallel read-only review agents, one per coherent file group, each paired with the docs/ADRs that describe its code. No source was modified. Baseline was green (`make build`, `go vet`, `gofmt -l`, `make test` all pass).
**Result:** **73 findings** (≈70 distinct; a few corroborated across units). The trust boundaries are, on the whole, **well built and well tested** — most CRITICAL/HIGH issues are latent (gated behind a default-off flag, an unwired path, or an uncommon input) rather than live exploits, but several are genuine soundness holes that contradict an ADR guarantee.

## Severity summary

| Severity | Count | One-line |
|---|---|---|
| 🔴 CRITICAL | 3 | stale build-cache false-pass; stale-overlay adjudication; `adopt` produces unloadable config |
| 🟠 HIGH | 8 | engine-child poison on real path; unprotected digest anchor; journal `0644`; extraction crashes/fact-loss; goal-memo replay; gdb hang |
| 🟡 MEDIUM | 26 | guard symlink gap; perfmcp unconfined/unvalidated; many doc/code drifts; durability gaps |
| ⚪ LOW | 36 | dead code, cosmetic doc drift, latent edges, count drift |

The most stale document is **`docs/design.md`** (an `index` subcommand that doesn't exist, a `runs:` schema the parser rejects, wrong runtime-layout paths). The strongest cluster of real bugs is in the **Python facts/runs engine** (build cache, overlay reconcile, AST extraction robustness).

---

## 🔴 CRITICAL

### C1 — Build cache serves a stale binary when the compile changes without a source edit → false pass
**`engine/arbiter_engine/runs/runner.py:164-173` (`_cache_key`), `build_cache.py:47-75` · ADR-0005 · unit 16 · reproduced**

`_cache_key` returns `f"{profile_key}:{target_id}:{stage}"` — it binds only sorted **profile names**, the target id, and the stage. It omits flag values, the `src_compile.cmd`/`pre`/`post` bytes, and any recipe digest. `build_cache.lookup` additionally validates only that the binary file exists and that `census.scan(root, sources).digest` (over the `sources:` globs — `.arbiter/` is excluded by the census walk) is unchanged. ADR-0005 (decisions.md:38-39) requires the build cache to **"key on full flags + profile."**

Reproduced two stale-serves with **no source file touched**:
- `env: CFLAGS` `-O0`→`-O2`: compile counter stayed at 1; the `-O0` binary was reused for the `-O2` request.
- `src_compile.cmd` changed to produce a different binary (`AAA`→`BBB`): the on-disk binary stayed `AAA`; the new command never ran.

**Why it's a trust hole:** during `recipe-derivation` the recipe book is *allowed* to drift (`internal/match/recipes_pin.go:70`, `allowBookDrift = hasCapability(...,"recipes")`). An author can prove a recipe whose `src_compile` builds a working binary, then edit the build to something weaker/different/broken and re-run: `run_stage` returns a cache hit, `gtest._boot_enumerate` enumerates the **stale** on-disk binary, and the suite runs it — so `build-booted`/`candidate-proven` pass against a build the current recipe would not actually produce.

**Fix:** fold the compile-determining inputs into the cache key (or a second validated digest): the resolved compile command + pre/post argv + effective `CFLAGS/CXXFLAGS/LDFLAGS` (or the serialized `src_compile`/`env`/applied-profile flags), not just profile names.

### C2 — Wired `reconcile` never invalidates a stale overlay pointer → referee adjudicates on stale facts
**`engine/arbiter_engine/facts/incremental.py:196-240`, `:768-805` · driven by `facts/view.py:73` · ADR-0018/0009 · unit 15 · reproduced**

The production writer path builds a **fresh** coordinator on every call (`view.py:73`), so `self._active_overlay` is always `None`. The no-dirty branch of `_reconcile_current_sources` returns `IncrementalStatus("ready", base_snapshot_id)` **without** clearing the on-disk overlay pointer (`incremental.py:237-240`). The only code that clears `overlay/current.json` (`_drop_overlay`) is reachable solely from `stop()`, the in-memory revert path (guarded by `_active_overlay is not None`, never true here), and the GC — **none on the wired `reconcile_current_sources` path.**

Reproduced (fresh coordinator per call, mirroring `view.reconcile`):
- reconcile #1 (source dirty) → `state=overlay`, pointer written.
- reconcile #2 (source **reverted** to base content) → `state=ready, overlay_id=None`, **but the pointer file still exists** and `load_active_overlay()` returns the stale overlay (`fact_upserts=['fact:new']`, `source_tombstones={'source:a'}`).
- The rpc reader (`rpc/__init__.py:469-479`) then merges that stale overlay: the query returns `fact:new` and hides the true base `fact:old`, while evidence reports `view_state="base"`. The Go referee reads `result_count`/`reachable`/`complete` from this response → **fact predicates adjudicate on reverted-edit facts.**
- Widened: after a full rebuild publishes a new base snapshot, the stale pointer (pinned to the OLD snapshot) is still loaded and its tombstones hide the freshly-rebuilt facts.

This directly violates ADR-0018 / engine-facts.md:46-50,82 ("the synchronous reconcile aborts rather than serve a stale base view"; `{snapshot_id, overlay_id, view_state}` honest on every adjudicated query) and the proposal's "base+overlay == pure-base after full rebuild."

**Fix:** in `_reconcile_current_sources`, on the empty-inventory and no-dirty "ready" returns, clear any on-disk overlay (pointer + `overlays/` patchset) independent of the in-memory `_active_overlay`; **and** harden `load_active_overlay` to return `None` when the pointer's `base_snapshot_id != store.stats().snapshot_id` (see H2 — its companion).

### C3 — `arbiter adopt` emits an unparseable `facts.incremental: <bool>`; migrated config breaks the engine
**`internal/deploy/adopt.go:146-148` · `engine/arbiter_engine/config/__init__.py:258-261` · ADR-0018 · unit 2 · reproduced**

`renderFactsConfig` flattens the legacy `incremental.enabled` section to a top-level scalar: `fmt.Fprintf(&b, "  incremental: %t\n", incremental)` → `"  incremental: true"`. But ADR-0018 changed `facts.incremental` from a reserved bool to a **mapping** (`enabled`, `poll_interval_ms`, …), and `_parse_incremental` now calls `_require_mapping(node, "facts.incremental")`, which rejects a scalar. Feeding the migrated output for the test's own fixture through `parse_config` yields `ConfigError → line 3: facts.incremental must be a mapping`. Every subsequent engine call (facts, runs) then fails to load config.

The unit test `TestAdoptMigratesLegacyFixtures` (adopt_test.go:43) asserts the broken string `"incremental: true"` instead of round-tripping through the engine parser, so the suite stays green while `adopt` is broken. (The function's own comment cites ADR-0018 — the code drifted from the ADR it references.)

**Fix:** emit the nested mapping `incremental:\n    enabled: %t`, and update the test to round-trip the rendered config through the engine `parse_config`.

---

## 🟠 HIGH

### H1 — Go client rejects the engine's `indexer_unavailable` error kind → poisons the child on a real path
**`internal/engineclient/client.go:636-661` · `engine/.../errors.py:16` · ADR-0020 · unit 9**

`knownEngineErrorKind` lists 19 engine error kinds but omits `indexer_unavailable` — the sole gap vs the engine's `KNOWN_ERROR_KINDS`. When the engine returns it, `validateErrorKind` produces a plain (non-`*EngineError`) error, so `Call` runs `poisonLocked()` and kill-groups + respawns the child. But `indexer_unavailable` is the **ADR-0020 mandatory-index hard stop**, surfaced by `engine.Refresh()` which the referee calls before every fact predicate (`verify.go:291`). On any host where the index toolchain is unavailable (common — e.g. gcc-impossible Macs), the referee gets a generic `"unknown engine error kind"` string (losing the `toolchain_code`/`detail` ADR-0020 promises) **and needlessly poisons the engine child.** No test cross-pins the Go list against the engine's, so CI is green. **Fix:** add `"indexer_unavailable"` to the switch; add a test asserting the Go list equals the engine's error-kind set.

### H2 — `load_active_overlay` merges an overlay whose `base_snapshot_id` no longer matches the current snapshot
**`engine/arbiter_engine/facts/incremental.py:768-805` · unit 15 · reproduced**

The pointer persists `base_snapshot_id` but `load_active_overlay` reads only `overlay_id` and never checks it against `store.stats().snapshot_id`. After a rebuild changes the base, the overlay built over the *old* snapshot is still applied over the *new* base — surfacing deleted facts and hiding live ones. Cross-process readers (executor/referee engines) take **no overlay lock** on this read path, so even with C2's writer fix a reader can observe the pointer mid-rebuild. **Fix:** treat a `base_snapshot_id` mismatch as "no overlay" (return base view) in `load_active_overlay` and `read_published`.

### H3 — The `engine_digest` trust anchor (`engines.json`) is not protected from player edits
**`internal/engineclient/client.go:173-196` · gap in `guard.go:27-49` + `deploy.go:1184-1206` · ADR-0011 · unit 9**

`Spawn` gates digest verification entirely on `.arbiter/run/engines.json` (`mode != "embedded"` ⇒ verification skipped; `engine_digest` is the value `Verify` checks). But neither the guard zones nor the generated deny rules cover `.arbiter/run/` — `guard_test.go:47` even asserts other `.arbiter/` files are intentionally allowed. A player can flip `mode` off to disable verification or rewrite `engine_digest`. **Mitigated** (HIGH not CRITICAL): the `.arbiter/engine/**` tree itself *is* guarded, so tampering the engine then re-pointing the digest fails; rewriting only the digest yields a fail-closed mismatch. Still an integrity-anchor gap vs the ADR-0011 posture. **Fix:** add `Edit/Write(.arbiter/run/**)` to the deny rules and a `.arbiter/run/` guard zone.

### H4 — Match journal created world-readable (`0644`), not the documented `0600`
**`internal/journal/journal.go:24` · ADR-0008 (decisions.md:71), design.md:161 · units 4 + 10 (corroborated)**

`os.OpenFile(path, …, 0o644)` for `.arbiter/match/log/journal.jsonl`, with no `Chmod`. ADR-0008 and design.md both promise `0600` for the referee's full-fidelity forensic record; the interpose compile-journal correctly uses `0o600` (`cc.go:324`), proving the match journal is the unintended outlier. The PreToolUse guard fences model tool calls, not the OS permission bit. **Fix:** change to `0o600`.

### H5 — libclang error-recovery flagging by line number alone (no file scoping) → silent fact loss on partial TUs
**`engine/.../facts/extractor/code/toolchain.py:192-200` → `ast_backend.py:659,720-722` → `mapper.py:323` · engine-facts.md · unit 11 · reproduced**

`_diagnostic_lines` collapses all diagnostics into a single flat set of line numbers, **discarding each diagnostic's file**. The backend then marks any AST node whose `loc.line` is in that set as `containsErrors`, and the mapper blocks the whole subtree from extraction. So an error on line N in `a.c` causes every node at line N in any `#include`d header of the same TU to be dropped — losing its functions/records/fields/calls and suppressing header materialization. Line-number collisions between a `.c` and its headers are common. The production backend has no per-file scoping; the test that "covers" this passes a per-source set, masking the bug. **Fix:** make `_diagnostic_lines` return `Dict[file, Set[int]]` and test each node's line against the set for its own file.

### H6 — Unbounded recursion in the AST mapper crashes the whole snapshot on deeply-nested expressions
**`engine/.../facts/extractor/code/mapper.py:1119,1143,343` + `mapper_utils.py:720,818` · ADR-0020 · unit 12 · reproduced**

`_walk`/`_walk_dicts`/`_capture_lines`/`_annotate_relative_conditions`/`_contains_dict` are plain self-recursive over AST children, with no explicit-stack conversion and no raised recursion limit. A left-associative `a+b+c+…` chain of 1500 terms (the exact shape clang emits) raises `RecursionError` at depth 1500 (Python's default limit is 1000). Only `_RecoverableExtractError` is caught at the per-file boundary, so a `RecursionError` propagates out and **aborts the entire extraction** rather than skipping one TU — which, since the index is mandatory (ADR-0020), becomes a hard stop. Large DBMS targets (mysql/sqlite/leveldb — the stated domain) routinely contain generated parsers and big boolean/initializer expressions exceeding 1000 nesting levels. **Fix:** convert the hot walkers to explicit stacks, or wrap per-TU mapping so deep-recursion failures become `_RecoverableExtractError` (skip+record).

### H7 — Goal-memo digest folds the *frozen* recipe-book hash; census excludes `.arbiter/` → stale-PASS replay
**`internal/match/goal_memo.go:49-58,80-81` + `actions.go:557-561` · go-referee.md:76-77 · unit 5**

The memo digest folds `m.RecipesPin` (captured once at load, immutable) and a census that skips `.arbiter/`, so neither reflects the *current* `recipes.yaml`. In a `recipes`-capability match with `goal_memo` enabled and a `run`-kind `[SetGoal]`, a player can pass once, weaken the recipe body, and on a later round get a memo **HIT** that calls `settle(...true...)` — bypassing the frozen-test gate the non-memo path runs. **Mitigated** (HIGH not CRITICAL): `match.goal_memo` is default-off and no shipped opening combines a recipes capability with a run-goal — but it's a real soundness hole that contradicts the doc's conservativeness claim and would bite any custom recipes playbook. **Fix:** fold the live recipes-book SHA into the digest, or skip memo when the match has the `recipes` capability.

### H8 — A GDB process that dies mid-command hangs the tool for the full timeout and misreports `gdb_timeout`
**`engine/arbiter_engine/gdbmcp/sessions.py:120-142,168-192` · unit 19 · reproduced**

`_reader_loop` ends on stdout EOF setting `state="exited"` but never wakes `self._waiters`; `command()` only checks liveness *before* the blocking `waiter.get(timeout=…)`. A GDB/inferior crash mid-call (normal for a debugger) leaves the waiter blocked for the entire per-command timeout (up to 60 s), then returns a misleading "timed out waiting for GDB result" instead of `session_exited`. **Fix:** on EOF, drain `self._waiters` and push a sentinel error so blocked calls wake immediately as `session_exited`.

---

## Cross-cutting themes

1. **The guard/deny fence has gaps outside `playbook/match/engine/agents`.** Integrity-relevant files are player-writable: `engines.json` (H3), `config.yml`/`recipes.yaml` (unit 5 context), and the guard's `decidePath` doesn't `EvalSymlinks` so a pre-existing symlink into a guarded zone bypasses the fence (M, unit 10) — even though `decideFrozen` *does* harden against symlink aliases. Consider a `.arbiter/run/` zone, Edit/Write denies, and symlink-resolved path checks.
2. **Extraction robustness: one bad TU can sink the whole snapshot.** H5 (fact loss), H6 (recursion crash), and the oversized-relative-condition `StorageError` (M, unit 12) all bypass `_RecoverableExtractError` → ADR-0020 hard stop. Per-TU failures should degrade, not abort.
3. **Overlay/incremental staleness (C2 + H2)** is the single richest bug cluster; both stem from the on-disk pointer not being tied to the current snapshot and not being cleared on the wired path.
4. **Durability is detect-not-corrupt, not crash-durable.** Journal/state `atomicFile` skips parent-dir fsync (L, unit 4); snapshot publish has no fsync barrier (M, unit 13). Reads fail closed on torn data, so this is availability, not corruption — but the docs say "fsync'd".
5. **`docs/design.md` is the most drifted document** — see the MEDIUM doc table. `go-referee.md` (missing `boot` clause), `go-deploy.md` (deny-rule list), and `engine-runs.md` (phantom JSON output) also drift from code.
6. **perf-mcp is weaker than its gdb-mcp sibling** despite the shared "kept invariants" doc: no closed-schema enforcement and no `root`/`cwd` confinement (M×2, unit 19).
7. **A few tests assert the bug, not the behavior** — `TestAdoptMigratesLegacyFixtures` (C3) and the per-file scoping fake in H5 both pass *because* they encode the broken/over-narrow shape.

---

## 🟡 MEDIUM (26)

**Trust-boundary / behavior**

| # | Finding | Location | Fix |
|---|---|---|---|
| 8 | Seat forwards `"structuredContent": null` when engine omits it (typed-nil `RawMessage` defeats `omitempty`); latent — engine currently always supplies it | `internal/seat/seat.go:323-335` | build result without the field, set only in the non-nil branch |
| 10 | Guard `decidePath` doesn't `EvalSymlinks`; symlink into a guarded zone bypasses the fence | `internal/guard/guard.go:159-175` | resolve parent dir with `EvalSymlinks` before prefix compare |
| 4 | `writeState` commits `state.json` before `status.json`; a status-write failure reports `state_corrupt` after state already advanced | `internal/match/store.go:109-114` | stage both temps, rename after both durable; or treat status.json best-effort |
| 10 | `arbiter status` hard-fails when `python3` is missing but a runs DB exists (should degrade) | `internal/cli/status_report.go:243-267` | treat python-invocation failure as `rows=0` |
| 19 | perf-mcp doesn't enforce its declared closed input schemas (`additionalProperties:false`, min/max, required) — silently clamps/ignores | `perfmcp/mcp.py:74-83`, `tools.py:211-231` | validate `arguments` against `inputSchema` |
| 19 | perf-mcp doesn't confine top-level `root`/`cwd` to the project (only relative `paths[]`) | `perfmcp/analysis.py:813-816` | scope the doc claim to gdbmcp, or add opt-in confinement |
| 19 | `gdb_command` danger gate inspects only the first token (embedded-`\n` bypass surface; exploitability unproven) | `gdbmcp/tools.py:374-385` | reject newlines/control chars, or scan every token |
| 12 | Oversized relative condition raises `StorageError`, aborting the snapshot (no per-file recovery) | `mapper_utils.py:713` → `store/models.py:325` | budget the condition holistically; fall back to unconditional |
| 13 | Snapshot publish has no fsync/durability barrier (fail-closed on read, so availability not corruption) | `facts/store/store.py:200-211` | fsync staged files + dirs before rename, or document detect-not-corrupt |

**Doc/code mismatches**

| # | Finding | Doc → Code | Fix |
|---|---|---|---|
| 20 | `design.md` lists an `arbiter index` subcommand that doesn't exist (its own §5 says "planned") | design.md:108,99 → cmd/arbiter/main.go | mark `index` planned |
| 18/20 | `design.md` shows `runs:{harness defaults}` but the parser rejects any key under `runs:` *(corroborated by 2 units)* | design.md:137 → config/__init__.py:102,307 | `runs:{} (reserved; must be empty)` |
| 20/10 | `status.json`/`log/` documented at `.arbiter/` top level; actually under `.arbiter/match/` | design.md:57,162-164 + go-cli.md:20 → store.go:44, deploy.go:25 | move under `match/` in docs |
| 6 | `go-referee.md` run-expect grammar omits the implemented `boot` clause (the gear-up gate) | go-referee.md:31-32 → verify/typed.go:159-163 | add `boot?:{exited_zero?,exit_code?,listed_tests_min?}` |
| 13 | ADR-0009 names a `snapshot.lock` under `.arbiter/locks/`; the real store lock is `.arbiter/facts/run/storage.lock` | decisions.md:76-77 → store_events.py:154 | record the absorbed store's real lock path |
| 16 | `vars:` is parsed/documented but never expanded or consumed (dead config) | engine-runs.md:18-21 → recipes.py:154 | implement `${var}` or drop from schema |
| 17 | engine-runs.md claims a gtest JSON-output option that doesn't exist (XML-only; JSON keys rejected) | engine-runs.md:40 → gtest.py:218 | drop "(JSON opt-in)" |
| 18 | Pipeline extraction runs at full `cpu_count()`; the documented "cores/4 while compiler active" throttle (`pool_width`) is dead | engine-shared.md:34 → pipeline.py:170 | wire `pool_width`, or fix the doc |
| 18 | engine-core.md says role/seat come from "argv"; code reads `ARBITER_ENGINE_ROLE/SEAT` env | engine-core.md:27-28 → rpc/__init__.py:311 | fix the doc to name the env vars |
| 21 | m4-test-migration-map phase split wrong (`16/165`; actual `22/221` Phase-1 + `2/12` Phase-2) | m4-test-migration-map.md:124 → c2/ | correct the per-phase counts |
| 21 | Doc lists `test_config_incremental.py` as a Phase-2 c2 file; it's at `engine/tests/` top level | m4-test-migration-map.md:104-106 | annotate as new-native/top-level |
| 21 | Skipped detail source-diversity test with no equivalent coverage (only the missing-endpoint half is justified) | c2/test_mcp_relations.py:315-344 | re-author with resolvable endpoints |
| 2 | `adoptRecipes`/`adoptCipherConfig` overwrite user `recipes.yaml`/`config.yml` (init/derivation-then-adopt) vs documented "preserved user state" | adopt.go:112,129 | route through a conflict-aware path |
| 2 | `freeplay` declares `[Verify] gear-up-published` but never binds it with `[Submit]` → build gate gameable | templates/freeplay.md:16-26 | add the `[Submit]` line |
| 7 | Parser `validIdentifier` accepts Unicode letters/digits vs the documented ASCII `[A-Za-z0-9_-]+` | parse.go:750-761 → FORMAT.md:122 | ASCII regexp, or document Unicode |

(MEDIUM also includes the corroboration overlaps noted with "/".)

## ⚪ LOW (36) — by subsystem

**Go.** Deploy deny-rule list / engine-deny gating wording / ADR-0010 "add-if-missing" imprecision (unit 1, ×3); `recipes_pin.targets{id:sha256}` doc vs empty-string reality (unit 3); `atomicFile` no parent-dir fsync (unit 4); `MaxExpectClauses` declared-but-unenforced + `OrderedSteps()` map-random on rehydrated playbook (unit 7, ×2); `FactEvidence.Complete` defaults `true` when both `complete`/`truncated` absent — latent (unit 6); run/fact evidence-shape doc lag (unit 6); `engineclient` `_meta` doc says `round` vs `round_seq` + orphaned `.tmp-*` hashed by `Digest` (unit 9, ×2); go-interpose "journal flock" that doesn't exist (unit 10).

**Python.** libclang handle leak on version-mismatch + streaming generators reset `_finished` on consumer exception (unit 11, ×2); `macro` object_id folds traversal `ordinal` → cross-TU dedup fails (unit 12); `os.replace` raw `OSError` on manifest-less dir + dead log-degradation non-atomic rewrite + persisted `lock_state` always "held" (unit 13, ×3); `_current_snapshot_dir` missing `.strip()` + one-hop `complete:true` when display capped (unit 14, ×2); coordinator poll thread publishes overlays without the OVERLAY flock (unwired) + `_drop_overlay` wipes the whole `overlays/` dir (unit 15, ×2); proposal claims boot work unmerged though it's on main (unit 16); discovery `_TEST_BODY_SUFFIX` dead constant + "TestBody function facts" docstring (queries `_Test` type) + disabled tests counted as passed (unit 17, ×3); `locks.acquire` leaks handle on non-`BlockingIOError` flock + dead `_has_miss_marker` (unit 18, ×2); gdb bounded-deque event slicing shift under bursts + perf-mcp grandchild leak on timeout (unit 19, ×2).

**Docs/tests.** derive-split proposal status "pending merge" though landed (PR #131) + `design.md` `arbiter-engine/` vs `engine/arbiter_engine/` path (unit 20, ×2); per-file test-count drift + `test_mcp_relations (9)` but 2 skipped + `test_storage_view_model` warning-state path unexercised (unit 21, ×3).

---

## Verified sound (trust boundaries that held)

The adversarial passes confirmed these are correct and well-tested — worth recording so they aren't re-litigated:

- **Frozen-test integrity** (unit 3): triple-checked (pre-exec disk re-hash, post-exec re-hash, async compile-time digest), symlink-defended at register, round-seq-guarded — closes the weaken→compile→restore race. Matches ADR-0017.
- **Step→predicate binding & named-verify** (units 3, 5): enforced on the raw submitted spec, resolved against the frozen snapshot, never re-reads the playbook file. Rejects inline-spec-riding-a-ref and submitter `allow_overrides`.
- **Verdict engine** (unit 6): `expect[]` lists are genuinely closed; missing/nil evidence fails closed across run/fact/mcp; deny-self (mcp-kind) matches ADR-0006.
- **Seat RBAC** (unit 8): no privilege bleed; per-seat tool allowlists never overlap; key validation + `--root` (ADR-0014, cwd never load-bearing) + capability re-check under flock all correct.
- **Facts search honesty** (unit 14): `complete`/`total`/`reachable`/`truncated` honest across plain/closure/reachable/overlay; SQL fully parameterized (no injection); base-vs-overlay parity byte-identical; stale index rejected.
- **Engine RPC + spawn/reap** (unit 9): line framing has no `Scanner` 64KB trap; responses matched by integer id under a per-call mutex (no `_meta` mis-correlation); `Setpgid`+kill-group+stdin-EOF, no goroutine leak; digest verification fail-closed (ADR-0011).
- **Lock ordering** (unit 4): matches ADR-0009; Go side only ever takes `MatchLock` (no multi-lock deadlock); checkpoint gate seat-scoped; subagent stop-gate fail-open.
- **Runs lifecycle** (unit 16): proven/unproven preserved only on matching spec+sources digests under `BEGIN IMMEDIATE`; worker-lost race CAS-guarded; process-group SIGKILL + SIGALRM timeouts.
- **gtest verdict path** (unit 17): no path where a crashed/0-test/non-booting target yields a passing verdict; `boot_exit_code`/`listed_tests` stamped on every post-boot return; boot clause fail-closed in `CompareRun`.
- **Test suite** (unit 21): 443 Python tests pass, 0 tautologies/assertion-free; c2 = exactly 24 files/233 tests; Go trust-boundary tests carry real negative assertions; transcripts live-replayed; GC tests deterministic.

---

## Appendix — per-unit findings

Full per-unit findings (with reproduction notes and "verified sound" details) were captured during the review. Counts by unit:

| Unit | Area | C | H | M | L |
|---|---|---|---|---|---|
| 1 Deploy core | Go | · | · | · | 3 |
| 2 Adopt/intro/templates | Go | **1** | · | 2 | 1 |
| 3 Match dispatch & test-reg | Go | · | · | · | 1 |
| 4 Match state/store/locks | Go | · | · | 2 | 1 |
| 5 Match goals/memo/pin | Go | · | **1** | · | · |
| 6 Verify engine | Go | · | · | 1 | 2 |
| 7 Playbook parser | Go | · | · | 1 | 2 |
| 8 Seat MCP servers | Go | · | · | 1 | · |
| 9 Engineclient/embedded | Go | · | **2** | · | 2 |
| 10 CC/guard/cli/journal/main | Go | · | **1** | 3 | 1 |
| 11 Facts extractor AST | Py | · | **1** | · | 2 |
| 12 Facts extractor mapper | Py | · | **1** | 1 | 1 |
| 13 Facts store write | Py | · | · | 2 | 3 |
| 14 Facts store read/search | Py | · | · | · | 2 |
| 15 Facts incremental/overlay | Py | **1** | **1** | · | 2 |
| 16 Runs recipes/cache | Py | **1** | · | 1 | 1 |
| 17 Runs gtest/discovery | Py | · | · | 1 | 3 |
| 18 Engine core/shared | Py | · | · | 2 | 3 |
| 19 Companions gdb/perf | Py | · | **1** | 3 | 2 |
| 20 System-wide docs | Docs | · | · | 3 | 2 |
| 21 Test-suite integrity | Tests | · | · | 3 | 3 |
| **Total** | | **3** | **8** | **26** | **36** |
