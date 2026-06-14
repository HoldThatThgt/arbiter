# go-seat — `internal/seat`

## Identity
Per-seat MCP server processes: the only thing a model can talk to. Implements constructive RBAC
(a tool not registered for a seat does not exist), seat credentials, engine-child lifecycle, and
verbatim proxying of engine tools.

## Inherits
chess `internal/seat` (M1 verbatim), extended in M2/M3/M7.

## Public surface
`arbiter serve <player|curator|executor> [--root DIR]`. Player: spawned by Claude Code from
`.mcp.json`, session-resident, no credential. Curator/executor: spawned per-subagent via inline
`mcpServers` frontmatter in the agent files deploy writes; both require `ARBITER_SEAT_KEY`
(0600, gitignored) — wrong/missing key is refusal of service, the chess posture.
**The repo root is explicit (ADR-0014):** init writes `--root <abs>` into every entry; cwd is
only a hand-run fallback. Match state is file-shared across seat processes, so a cwd-derived
root makes curator-loaded matches invisible to the player whenever the host spawns the two
contexts with different cwds.

## Seat → tool registration
- **player (10):** ShowStepJob, CreateTask, CheckStepJob, SubmitCheckpoint, ListTask, ReviewTask,
  NotePlaybook, AddPlayBook, + proxied `search`, `detail`.
- **curator (4):** ReadPlayBook, LoadPlayBook, ListTask, ReviewTask.
- **executor (8 base + 3 gated):** SubmitTask, RegisterTest, ListTask, ReviewTask, `search`,
  `detail`, `run`, `recipe_search` (renamed from crun's `search` — the only name collision in the
  bundle); gated: `register`, `import_recipes`, `scan` — registered ONLY when the loaded playbook
  declares `capabilities:[recipes]`.
- Capability-gating edge semantics are **fail-closed**: executor seat with no active match at
  birth registers NO gated tools; every gated call re-checks under flock that the granting match
  is still current, else `capability_revoked`.

## Engine children
- Each seat owns up to two engine child processes (see go-engineclient for protocol):
  **QUERY** (facts) — spawned eagerly at seat birth (cheap: engine defers overlay reconcile until
  first fact access); **EXEC** (runs) — spawned lazily on first run-tool or run-predicate use.
  The split removes head-of-line blocking: an hour-long goal run never blocks `search`.
- `tools/list` for proxied tools is forwarded from the live QUERY engine — Go never pins engine
  tool schemas, so drift is structurally impossible; golden transcripts gate field shapes in CI.
- Proxying is verbatim: request arguments pass through untouched; correlation
  `{match_id, round, task_id}` rides JSON-RPC `_meta`, NEVER tool arguments (cipher's closed
  schemas must survive unmodified).
- Lifecycle: children are placed in their own process group (`Setpgid`); seat exit kills the
  group; engines self-exit on stdin EOF. Darwin has no parent-death signal — the EOF path is the
  load-bearing one and must be torture-tested (kill -9 the seat, verify no orphan within 2s).
- Facts single-writer rule (ADR-0009): only the **player's** QUERY engine reconciles/publishes
  overlays; executor engines read base + latest published overlay.

## Invariants
Constructive RBAC (registration is the security boundary, not advice); seat key required for
privileged seats; no daemon — children live strictly within seat lifetimes; stdio only.

## Tests
Registration matrix per seat (golden tools/list per seat per capability state); key
refusal; gated-tool revocation race (match replaced while call in flight → `capability_revoked`);
orphan-reaping torture on darwin and linux; proxy passthrough byte-equality (transcripts);
`_meta` stamping presence/shape; QUERY/EXEC isolation under a long-running fake build.

## Done
M1 port-in → M2 engine children + proxying → M3 `_meta` + run/fact evaluation routing →
M7 capability gating. Registration or credential changes are `needs-human`.
