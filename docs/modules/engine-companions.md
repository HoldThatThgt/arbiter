# engine-companions — `engine/arbiter_engine/{gdbmcp,perfmcp}`

## Identity
The bundled diagnostic MCP servers (ADR-0010): `gdbmcp` exposes structured GDB/MI debugging
(sessions, run control, breakpoints/watchpoints, stack/locals/registers, bounded memory reads,
guarded console), `perfmcp` exposes C performance triage (ranked static findings, rule
explanations, argv-only command measurement, toolchain probe). They ship inside `arbiter-engine`
so delivering arbiter delivers them — but they are NOT engine JSON-RPC namespaces: each is a
self-contained stdio MCP server with its own line loop, spawned per-use from `.mcp.json` via the
engine interpreter (`python3 -m arbiter_engine.gdbmcp|perfmcp`), never via the arbiter binary
(deny-self, ADR-0006).

## Inherits
`gdb-mcp` and `perf-mcp` source checkouts, imported one-way (source repos frozen for new
features, same posture as cipher's M4 import). Relative-import packages — moved verbatim except:
standalone `init` subcommands dropped (go-deploy owns all Claude Code wiring), CLI prog strings
renamed. Their hermetic test suites (fake GDB/MI subprocess, fake workloads, transcript replay)
are ported into `engine/tests/` as `test_gdbmcp_*` / `test_perfmcp_*`; the live-GDB smoke test
is deliberately NOT ported (real-toolchain tests are a separate, explicitly-marked CI job per
PROCESS.md).

## Public surface
- **gdbmcp** (server name `gdb-mcp`, 12 tools): `gdb_start`, `gdb_exec`, `gdb_breakpoint`,
  `gdb_select`, `gdb_stack`, `gdb_snapshot`, `gdb_eval`, `gdb_memory`, `gdb_command`,
  `gdb_sessions`, `gdb_stop`, `gdb_diagnostics`. Attach/remote/outside-root/dangerous console
  commands are opt-in serve flags, denied by default; `--no-audit` disables the redacted
  `.gdb-mcp/audit.jsonl` log. State + audit under repo-local `.gdb-mcp/`.
- **perfmcp** (server name `perf-mcp`, 4 tools): `perf.scan_c`, `perf.explain_finding`,
  `perf.measure_command` (argv arrays only — shell strings rejected), `perf.toolchain_probe`.
  Results are schema-versioned (`perf-mcp.scan.v1`, …) with stable rule ids.
- CLIs: `python -m arbiter_engine.gdbmcp serve|doctor`, `python -m arbiter_engine.perfmcp
  serve|scan|probe|measure|tools`. No `init` — wiring is go-deploy's.

## Design
Every tool returns typed `structuredContent` plus a display-only text summary — the property
mcp-kind `expect[]` predicates adjudicate against (`{path, op: eq|ne|ge|le|exists, value}`,
go-referee.md). Both servers keep their upstream invariants: closed input schemas, bounded
outputs/timeouts, root-confined paths, fail-closed errors, no runtime network, no daemons.
Executors (typically the deploy-written `arbiter-debugger` agent) use the tools for evidence;
the referee compares the fields.

## Invariants
stdlib-only (AST meta-test covers both namespaces — `resource` is POSIX stdlib in lib-dynload);
`search`/`detail` freeze untouched (companions add no facts-namespace tools); companion server
processes hold no referee or evaluator state; `.mcp.json` entries always launch via the engine
interpreter so the ADR-0006 deny-self guard is never in play.

## Tests
Ported hermetic suites: MI parser, fake-GDB sessions (breakpoints, watch, snapshot,
outside-root denial, opt-in remote), MCP protocol + closed-schema validation, stdio subprocess
round-trip, byte-exact session transcript replay, doctor degradation; perf scanner rules on
fixture C, measure argv discipline, scan→explain cache flow, CLI measure. Unit CI never invokes
a real gdb/cc.

## Done
Imported with ADR-0010. Schema or tool-surface changes follow the upstream servers' compat
rules (new schema version for breaking changes); registration into agents/`.mcp.json` is
go-deploy's contract.
