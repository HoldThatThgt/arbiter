# go-cli — `cmd/arbiter`

## Identity
The single entrypoint. Subcommand router plus the two read-side commands (`status`, `report`).
Everything heavy lives in `internal/*`; this module stays thin.

## Public surface
| Command | Who | Module |
|---|---|---|
| `arbiter init` / `arbiter adopt` | user | go-deploy |
| `arbiter serve <seat>` | Claude Code / agent frontmatter | go-seat |
| `arbiter hook stop\|guard\|subagent-stop` | Claude Code hooks | go-referee + internal/guard (gate checks; fail-open) |
| `arbiter cc -- …` | build systems via recipes | go-interpose |
| `arbiter status [--json]` | user (spectating) | this module |
| `arbiter report [--json] [match_id]` | user (spectating) | this module |
| `arbiter index [--rebuild] [--compile-database P]` | CI/recovery (planned) | engine batch mode — **not yet wired as a Go subcommand** |

## Design
- **status:** compose-on-read (ADR-context: single-composer rule). The referee owns
  `.arbiter/match/status.json` (match projection only, 0644, never future steps); `arbiter status`
  reads it and *queries* the engine for facts/runs status (snapshot id + age, staleness flags,
  proven-recipe counts, engines.json verification state) — two processes never write one file.
- **report:** post-match digest joining `journal.jsonl` (full fidelity) with runs SQLite rows on
  correlation IDs `{match_id, round, task_id, run_id}` — per-task run history, failing→passing
  arcs, gotcha hits. Output: human text or `--json`.
- **index:** *(planned — not yet implemented as a CLI subcommand)* would spawn the engine in
  batch mode for headless CI extraction and disaster recovery; hidden from interactive docs, not
  a user verb (ADR-0004). Build-driven indexing (gear-up) is the only path that ships today.
- **hook:** dispatches three fail-open subcommands — `stop` (Stop-hook checkmate/budget gate,
  exit-code contract identical to chess, state read under flock), `guard` (the PreToolUse path
  fence, `internal/guard`, ADR-0015), and `subagent-stop` (executor-subagent submission gate). A
  broken referee must never trap the user's session, so all three fail open.

## Invariants
No business logic in `cmd/`; every subcommand's output has a `--json` form with a frozen schema
(documented in this file as they land); exit codes: the Go subcommand router exits 1 for every
error — both typed operational errors and usage errors (`cmd/arbiter/main.go`). Exit 2 on usage is
specific to the `arbiter cc` shim (`internal/interpose/cc.go`), not a router-wide contract.

## Tests
Status composition with each subsystem absent/stale; report join correctness on a fixture
journal+sqlite pair. *(Planned: CLI golden tests for cmd/arbiter — args → exit code + stdout
schema — against a fake engine; not yet implemented.)*

## Done
Skeleton in M1 (serve/hook), status/report in M7, cc in M6, index in M4.
