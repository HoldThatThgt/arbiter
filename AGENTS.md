# Repository Guidelines

On any conflict, `PROCESS.md` and `docs/modules/*.md` win over this file.

## Structure & Authority

Read before work: `PROCESS.md`, `docs/migration.md`,
`docs/modules/<module>.md`, `docs/design.md`, then `docs/decisions.md`. Module
specs are binding; `docs/design.md` is context, and `docs/decisions.md` is the
owner-signed ADR log. Current scaffold layout: Go entrypoint in `cmd/arbiter`,
Go packages in `internal/*`, Python in `engine/arbiter_engine/*`, tests in
`engine/tests`, and future golden transcripts in `testdata/transcripts/`.

## Build, Test, and Development Commands

- `make build` - build the `arbiter` Go binary.
- `make test` - run `go vet`, `go test -race ./...`, and `unittest`
  discovery with `PYTHONPATH=engine`.
- `make test-py` - run only the Python engine tests, including the stdlib AST
  import meta-test.
- `make fmt-check` - fail if any Go file needs `gofmt`.
- `git diff --check` - catch whitespace issues.
- `rg "<term>" docs PROCESS.md README.md` - find governing spec text.

## Implementation Workflow

Work only in the current milestone. Take the oldest `ready` issue, comment, and
branch `issue/<n>-<slug>`. Implement only that scope; file adjacent problems
separately. Use TDD: show the failing test before the fix. Keep one concern per
PR and never merge your own PR or self-assess a milestone.

## Coding Style & Invariants

Keep Markdown headings concise and stable. Go uses `gofmt`, vendored deps, and
table-driven tests. Python is >=3.9 stdlib-only; new dependencies need an ADR.
Referee and evaluator logic must be deterministic: no wall-clock branching,
randomness, or map-order dependence. Adjudication consumes typed fields and
counters only, never prose or string patterns. Fail closed except the Stop hook
and `arbiter cc`; runtime is repo-local, stdio-only, no daemons or network.

## Testing Guidelines

Go referee changes need public two-phase protocol scenario tests. Python uses
stdlib `unittest`; unit CI never invokes a real compiler or gtest binary. Any
JSON-RPC shape change must update golden transcripts in the same PR. Keep
`search`/`detail` schemas byte-frozen.

## Commit & Pull Request Guidelines

Git history uses prefixes such as `Design: ...`; keep subjects short. PR titles
use `<module>: <what>`. Bodies must include `WHAT`, `WHY`, `SPEC`, `TESTS`, and
`RISKS`, quote module-doc lines, and end with `Closes #<n>`.

## Escalation & Owner Gates

If a spec is wrong, ambiguous, or must change, open a `needs-decision` issue
using `DECISION-REQUEST`, then move to the next ready issue. M1 chess
and M4 cipher-2 imports are owner-gate steps. Docs travel with code: behavior
changes described by a module spec update that spec in the same PR.
