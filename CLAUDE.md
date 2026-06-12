# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Authority and process

On any conflict: `PROCESS.md` and `docs/modules/*.md` win, then `docs/design.md` (the spec of record), then `docs/decisions.md` (owner-signed ADR log — specs change only through it). `AGENTS.md` summarizes repo guidelines. Work is issue-driven (one issue → branch `issue/<n>-<slug>` → one PR, ≤~400 net non-test lines, TDD with the failing test shown first); the PR body format (WHAT/WHY/SPEC/TESTS/RISKS) is mandatory.

## Commands

```sh
make build        # go build ./cmd/arbiter
make test         # test-go + test-py (the CI gate)
make test-go      # go vet ./... && go test -race ./...
make test-py      # PYTHONPATH=engine python3 -m unittest discover -s engine/tests
make fmt-check    # fails if any Go file under cmd/ or internal/ needs gofmt
make transcripts  # regenerate testdata/transcripts/*.jsonl from the Python engine
```

Single tests:

```sh
go test -race ./internal/match -run TestName
PYTHONPATH=engine python3 -m unittest discover -s engine/tests -p 'test_census.py'
```

Full pre-push gate: `make build test fmt-check && python3 -m compileall -q engine/arbiter_engine engine/tests && git diff --check`.

## Architecture

Two artifacts, one seam:

- **`arbiter`** (Go, `cmd/arbiter` + `internal/*`, vendored deps only): deterministic referee, per-seat MCP servers, deploy, Stop-hook gate, and the `arbiter cc` compiler shim.
- **`arbiter-engine`** (Python ≥3.9, `engine/arbiter_engine`, **stdlib-only** — new deps need an ADR): `facts/` (typed-AST extraction, snapshots, search/detail), `runs/` (recipes, gtest adapter, async runs), `shared/` (census, locks, build pipeline).

They speak line-delimited JSON-RPC over stdio. `internal/engineclient` spawns `python -m arbiter_engine.rpc` per role (`query`/`exec`), poisons the child on any protocol failure, and seats respawn poisoned engines. Env contract: `ARBITER_BIN` (absolute binary path, set at spawn so compile stages can invoke `arbiter cc` off PATH), `ARBITER_ENGINE_CALL_TIMEOUT_S` (default 600s call deadline), `ARBITER_ENGINE_PYTHON` (interpreter resolution, before `PYTHON`). Embedded-engine mode (`internal/embeddedengine`) unpacks a digest-verified copy of the engine tree into `.arbiter/engine`.

**The wire contract is pinned by golden transcripts.** `engine/tests/write_transcripts.py` generates `testdata/transcripts/*.jsonl`, replayed by both runtimes (Go: `internal/engineclient` replay test). Any JSON-RPC or tool-schema change must regenerate transcripts in the same change (`make transcripts`). `search`/`detail` schemas are byte-frozen.

**Seats and match state.** `arbiter serve player|curator|executor` exposes role-scoped MCP tools (`internal/seat`) — RBAC by construction. Match state lives in `.arbiter/match` behind a file lock (`internal/match` store, `withLock`); every transition appends to a journal. The model has no "declare success" interface: adjudication consumes typed fields and counters only, never prose.

**Verification.** `internal/verify` evaluates typed predicates (`shell`/`mcp`/`run`/`fact`) with `expect` clauses compared field-by-field (`typed.go`). Anti-false-checkmate machinery spans several packages and must stay coherent: recipe pinning (`internal/match/recipes_pin.go` — snapshots `recipes.yaml` at `LoadPlayBook`), named `[Verify]` predicates (snapshotted into match state, resolved by name at `SubmitTask`; `verify_policy: named` frontmatter forces curated predicates), and goal memoization (census digest recomputed post-run before memoizing).

**Cross-runtime contracts that must agree on both sides** (Go and Python tests each cover only their half; the seam is where bugs hide):
- `recipes.yaml` is RecipeBook v2: `targets:` is a **sequence** of `- id:` entries. `engine/arbiter_engine/runs/recipes.py` is the canonical parser; `internal/match/recipes_pin.go` and the deploy default must parse/emit the same form.
- gtest run results serialize per-test outcomes as a `per_test` array (`runs/gtest.py RunResult.to_json`); Go (`goals_async.go`) converts to the `Suite.Name → status` map that `CompareRun` consumes.
- Response-file splitting: Go `internal/interpose/cc.go` mirrors Python `shlex.split` semantics.
- Playbook format (sections `[SetGoal]`/`[Verify]`/`[STEP]`...) is documented in `internal/deploy/templates/FORMAT.md`; opening templates live beside it.

**Facts pipeline ("compile done ⇒ index done").** The index has no standalone lifecycle: `arbiter cc` (`internal/interpose`) journals every compiler invocation per build id; `engine/arbiter_engine/shared/pipeline.py` consumes the journal after a green build — compile_db → extract cache (semantic keys include `-fsanitize=*` flags and a repo-wide headers digest) → published snapshot under `.arbiter/facts/snapshots/current`. The journal is truncated per build by the runner; failed extractions are never persisted as cached.

**Runs.** Synchronous runs go through the engine `run` tool; async runs (`runs/async_runs.py`) double-fork a detached worker recording to sqlite (`.arbiter/runs/state.sqlite`) with a SIGALRM deadline from the spec `timeout_s` and worker-pid liveness checks in `runStatus`. The Go side polls via `CheckStepJob` while `GoalPending` is set; `state:"unknown"` is retryable, never a verdict.

## Invariants (red lines)

- Referee and evaluator logic must be deterministic: no wall-clock branching, no randomness, no map-order dependence.
- Fail closed everywhere except the Stop hook and `arbiter cc` (which must never break a user's build).
- Runtime is repo-local (`.arbiter/`), stdio-only — no daemons, no network.
- Go: vendored deps, `gofmt`, table-driven tests. Python: stdlib-only, `unittest`; unit CI never invokes a real compiler or gtest binary.
- Committed knowledge is `playbook/*.md` + `recipes.yaml` + `config.yml`; everything else under `.arbiter/` is runtime state.
