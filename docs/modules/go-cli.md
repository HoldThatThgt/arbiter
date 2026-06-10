# go-cli — `cmd/arbiter`

## Identity
The single entrypoint. Subcommand router plus the two read-side commands (`status`, `report`).
Everything heavy lives in `internal/*`; this module stays thin.

## Public surface
| Command | Who | Module |
|---|---|---|
| `arbiter init` / `arbiter adopt` | user | go-deploy |
| `arbiter serve <seat>` | Claude Code / agent frontmatter | go-seat |
| `arbiter hook stop` | Claude Code Stop hook | go-referee (gate check; fail-open) |
| `arbiter cc -- …` | build systems via recipes | go-interpose |
| `arbiter status [--json]` | user (spectating) | this module |
| `arbiter report [match_id]` | user (spectating) | this module |
| `arbiter index [--rebuild] [--compile-database P]` | CI/recovery ONLY | engine batch mode |

## Design
- **status:** compose-on-read (ADR-context: single-composer rule). The referee owns
  `.arbiter/status.json` (match projection only, 0644, never future steps); `arbiter status`
  reads it and *queries* the engine for facts/runs status (snapshot id + age, staleness flags,
  proven-recipe counts, engines.json verification state) — two processes never write one file.
- **report:** post-match digest joining `journal.jsonl` (full fidelity) with runs SQLite rows on
  correlation IDs `{match_id, round, task_id, run_id}` — per-task run history, failing→passing
  arcs, gotcha hits. Output: human text or `--json`.
- **index:** spawns the engine in batch mode for headless CI extraction and disaster recovery.
  Hidden from interactive docs; not a user verb (ADR-0004). Prints the same `facts:{…}`
  accounting a gear-up verdict would carry.
- **hook stop:** exit-code contract identical to chess; fail-open (a broken referee must not
  trap the user's session); checkmate/budget state read from match state under flock.

## Invariants
No business logic in `cmd/`; every subcommand's output has a `--json` form with a frozen schema
(documented in this file as they land); exit codes: 0 ok, 1 typed operational error, 2 usage.

## Tests
CLI golden tests (args → exit code + stdout schema) with a fake engine; status composition with
each subsystem absent/stale; report join correctness on a fixture journal+sqlite pair.

## Done
Skeleton in M1 (serve/hook), status/report in M7, cc in M6, index in M4.
