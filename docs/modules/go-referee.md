# go-referee — `internal/match`, `internal/verify`, `internal/playbook`, `internal/journal`

## Identity
The deterministic core. Owns match state, step progression, task verdicts, goal settlement, and
the journal. Pure counting under flock with two-phase lock protocols; no model output ever
adjudicates. This module is chess kept nearly verbatim, extended with a typed predicate language.

## Inherits
chess `internal/match`, `internal/verify`, `internal/playbook`, `internal/journal` — imported in
M1 verbatim (rename + path relocation only), then extended in M3+. The state machine, two-phase
protocols (in-lock validate → out-of-lock execute → re-lock settle with `round_seq` guard),
flock + atomic dual-write, append-only gotcha mechanics, and Stop-gate/`[SetGoal]`/`max_steps`
semantics are **not redesigned**.

## Public surface (MCP tools, registered per seat by go-seat)
`ShowStepJob`, `CreateTask{request, fact_refs?≤8, verify?}`, `CheckStepJob`, `ListTask`,
`ReviewTask`, `NotePlaybook`, `AddPlayBook`, `ReadPlayBook`, `LoadPlayBook`,
`SubmitTask{task_id, summary≤1024B, report, result:ResultSpec}`.

## Design

### ResultSpec — the predicate language
```
{kind:"shell", command}                                   # escape hatch, unchanged semantics
{kind:"mcp", server, tool, arguments,
 expect?:[{path, op: eq|ne|ge|le|exists, value}]}         # FOREIGN servers only; ≤8 clauses,
                                                          # scalars, closed ops, no wildcards
{kind:"run", recipe?, tests:[...], options?,
 expect:{overall: enum|one_of[...], max_failed?, min_passed?, test?:{name,result}}}
{kind:"fact", query:"<search mini-language>",
 expect:{min_results?, max_results?, complete?, reachable?, total_at_least?}}
+ timeout_s (default 600, max 3600), output_lines
```
- Closed key sets; unknown keys are validation errors at submit time (fail-closed).
- `run`/`fact` kinds are evaluated via the seat's engine children (go-engineclient); they never
  route through `.mcp.json`. Field comparison is typed (`expect` vs structured result), and the
  per-clause `expect_report [{path,op,value,actual,ok}]` is stored on the Task and surfaced by
  `ReviewTask`.
- **Deny-self guard (ADR-0006):** any mcp-kind target whose resolved command
  (LookPath → Abs → EvalSymlinks) equals `os.Executable()` → `reserved_server`, full stop.
- `Result.evidence` is typed per kind: run → `{run_id, overall, passed, failed,
  first_failure_name}`; fact → `{snapshot_id, overlay_id, view_state, result_count, complete}`.
  **Adjudication consumes only verdict enums and counters; evidence enriches review, never the
  verdict.**

### Playbook extensions (`internal/playbook`)
- `[Verify]` blocks: named predicate specs in the playbook trust domain. Executors reference them
  by name (`verify:"repro-passes"`); the referee resolves the frozen spec — an executor cannot
  author its own expect for a step that declares verification.
- `[SetGoal]` accepts run/fact kinds. `capabilities:` frontmatter (e.g. `[recipes]`) drives
  capability-gated tool registration (see go-seat).
- Tokenizer stays regex-free; grammar changes require fixture updates in the same PR.

### Match-state extensions (`internal/match`)
- `Match.recipes_pin{book_sha256, targets{id:sha256}}` written at LoadPlayBook; run-kind
  predicates verify the pinned hash before execution and fail with journaled
  `recipe_pin_mismatch` on drift.
- `Task.briefing[]` (resolved fact cards, ≤8KB total, pruned from archived rounds — the journal
  retains them), `Result.evidence`, `expect_report[]`, `Match.goal_memo`, `Match.goal_pending`.
- Async goals: first `CheckStepJob` after step success starts the goal via `arbiter/startRun`
  and returns `{complete:false, reason:"goal_running", run_id}`; later calls poll
  `arbiter/runStatus` and settle two-phase (existing `round_seq` machinery — a round that moved
  invalidates the pending settle).
- Goal memoization (ADR-0005): digest = sha256(sorted(path,hash) over goal census scope) folded
  with toolchain hash, goal-spec hash, recipe-book hash. **Default-off** (`match.goal_memo:
  false`) until the property suite (cached-vs-forced equivalence, adversarial new-file cases)
  proves conservativeness. Memoized passes journal `goal_checked{memoized:true}`.
- Referee triggers `arbiter/refresh` (deduped per round) before evaluating fact-kind predicates
  so adjudication never reads a stale overlay.

### Fixes carried from the audit
Curator `ListTask` whitelist drift; dead `AbortReplaced`/`AbortInternalError` constants removed.

## Invariants
No model-declared success; pure counting; lock protocols untouched; natural language never
adjudicates; journal is append-only, full-fidelity (ADR-0008), fsync'd, 0600; `status.json`
contains the referee-owned match projection only and never future steps.

## Tests
- chess's existing suite + race suite green at every PR (M1 baseline).
- Adversarial deny-self matrix: symlinked binary, renamed copy, argv injection, relative paths.
- expect-clause property tests: every op × type mismatch × missing path → fail-closed.
- False-checkmate kill test (M3 exit): structured `{overall:"failed"}` with `isError:false`
  cannot satisfy `expect:{overall:"passed"}`.
- Async-goal settle across seat restart; memoization equivalence suite (M7).
- Pin mismatch: recipe edited after LoadPlayBook → `recipe_pin_mismatch`, journaled.

## Done
M1 port-in green → M3 typed predicates + guard + pinning → M7 briefings/[Verify]/memoization.
Any change to verdict semantics is `needs-human`.
