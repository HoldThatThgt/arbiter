# engine-core — `engine/arbiter_engine/{rpc,config,log,errors}`

## Identity
The Python engine's chassis: stdio JSON-RPC loop, namespace router, config, typed errors,
logging. Python ≥3.9, **stdlib-only** (enforced by the AST meta-test — generalized from crun's
`no-import-re` pattern; the optional `[scan]` extra is the single sanctioned exception and must
fail closed when absent).

## Inherits
cipher-2 `mcp/` server loop (line-delimited JSON-RPC, request validation, descriptor pattern,
response budget shaping) — refactored into an engine-wide core hosting multiple namespaces.
cipher-2 `config/` and `tools/log` — refactored. crun `audit.py` redaction — merged as the runs
channel policy.

## Public surface
- **MCP tools** (namespaced registries): `facts`: `search`, `detail` (schemas byte-frozen);
  `runs`: `run`, `recipe_search`, `register`, `import_recipes`, `scan`.
- **Custom JSON-RPC methods** (referee-only, never in tools/list): `arbiter/refresh`,
  `arbiter/census`, `arbiter/resolveBriefing`, `arbiter/startRun`, `arbiter/runStatus`,
  plus a version/handshake method for engines.json verification.
- **Batch mode**: `python -m arbiter_engine index|status` for CI/recovery (go-cli wraps it).

## Design
- One JSON object per line; one in-flight request; stderr = logs only. `_meta` extracted at the
  chassis and passed to handlers as context — tool argument schemas stay closed
  (`additionalProperties:false`, `_reject_unknown_args` preserved from cipher).
- Roles: the chassis knows whether it was spawned as QUERY or EXEC and as which seat's child
  (argv), and enforces the single-writer rule (only player-QUERY may reconcile/publish overlays).
- Typed error taxonomy (JSON-RPC error.data.kind): `no_snapshot{hint}`, `briefing_unresolved
  {bad_refs[]}`, `capability_revoked`, `recipe_pin_mismatch`, `engine_stale{expected,found}`,
  `harness_unavailable`, `lock_timeout{lock}`. New kinds require a doc update here + transcripts.
- Config: `.arbiter/config.yml`, strict YAML-subset parser (fail-closed dialect, line-precise
  errors; crun's recipe corpus as golden tests). Sections: `facts:{extractor, incremental,
  index_on_build:{pool, key_flags}}`, `runs:{...}`, `match:{goal_memo}`, `engine:{}`.
- Logging: channel files `facts.jsonl` / `runs.jsonl` under `.arbiter/log/`, cipher's redaction
  discipline (runs channel: length-only payload summaries per crun); every event carries
  available correlation IDs. The referee's `journal.jsonl` is NOT written here (ADR-0008).
- Long work (`startRun`): double-forked, process-grouped worker bounded by its own timeout,
  writing results to runs SQLite — a bounded job, not a daemon; settle-able across seat restarts.

## Invariants
stdlib-only; schemas closed; budget ladder (8/32/128KB small/normal/large) is the engine-wide
response shaper for any sizable payload; no network; repo paths only under `.arbiter/`.

## Tests
Loop framing fuzz (oversize lines, invalid JSON, unknown methods → typed errors); meta-test for
imports; config parser golden corpus incl. hostile YAML; redaction unit tests (secret-shaped
values never appear in channel logs); handshake/staleness; double-fork worker lifecycle.

## Done
M2 (chassis + handshake + transcripts v1) → grows per namespace. Error-taxonomy or schema
changes regenerate transcripts in the same PR.
