# go-engineclient — `internal/engineclient`

## Identity
A deliberately minimal, hand-rolled line-delimited JSON-RPC 2.0 stdio client (~300 LOC target).
The single seam between the Go binary and the Python engine. Also home of the golden-transcript
contract-test harness that pins that seam forever.

## Why hand-rolled (ADR-0002 context)
The vendored go-sdk cannot issue custom (non-MCP) JSON-RPC methods and its jsonrpc2 package is
`internal/`. The engine's server loop is itself a hand-rolled line loop (cipher's), so the
matching client is the smallest honest option. Alternative (key-gated referee tools on the MCP
surface) was rejected: referee capability never lands on a model-facing surface.

## Public surface (Go API)
```go
type Engine struct{ ... }                       // one child process
Spawn(ctx, role EngineRole, repo string) (*Engine, error)   // QUERY | EXEC
e.CallTool(name string, args, meta any) (ToolResult, error) // MCP tools/call passthrough
e.ToolsList() ([]ToolDecl, error)                           // forwarded, never cached across spawns
e.Refresh(scope) / e.Census(scope) / e.ResolveBriefing(refs)
e.StartRun(spec) (runID, error) / e.RunStatus(runID)        // arbiter/* custom methods
e.Close()                                                    // EOF stdin, wait, kill-group fallback
```

## Design
- Transport: one JSON object per line on stdin/stdout; stderr is engine logging, passed through
  to the seat's stderr (never parsed).
- Requests carry `_meta:{match_id?, round?, task_id?}`; responses are validated for `id` match
  and JSON-RPC error shape; engine-typed errors (`no_snapshot`, `briefing_unresolved`, …) are
  surfaced as typed Go errors, not strings.
- Timeouts per call (default 600s, max 3600s aligned with ResultSpec); a timed-out call poisons
  the child (kill-group + respawn by the seat) — no half-synchronized reuse.
- Concurrency: one in-flight request per child connection by design (the engine is a line loop).
  Long operations go through `startRun`/`runStatus` polling instead of long-blocking calls; the
  QUERY/EXEC child split (go-seat) handles the rest. No pipelining cleverness.

## Golden stdio transcripts — the permanent contract
- `testdata/transcripts/*.jsonl`: recorded request/response exchanges, one scenario per file
  (every tool, every custom method, every typed error, every budget-degradation tier).
- CI replays every transcript two ways: (1) Go client sends recorded requests to the REAL Python
  engine and asserts response shape/field equality (allowlist for volatile fields: ids, paths,
  timings); (2) the Python engine's own harness replays them against its loop.
- Any PR that changes traffic shape regenerates transcripts in the same PR; the regeneration
  diff is review surface. Transcript edits without a corresponding spec/doc change are rejected.
- This is what makes ADR-0002's escape hatch real: behavior is pinned at the seam, so a future
  engine port is a refactor against frozen transcripts.

## Invariants
No third dependency for the protocol; no model-facing exposure of `arbiter/*` methods; stderr
never parsed; typed errors only.

## Tests
Unit: framing (huge lines, split reads, invalid JSON, id mismatch), timeout/poison/respawn,
EOF handling. Contract: full transcript corpus both directions. Fault injection: engine that
hangs, engine that emits garbage line mid-stream, engine that dies mid-call.

## Done
M2 (client + corpus v1) → grows with every engine-surface PR thereafter. The transcript harness
itself changing is `needs-human`.
