# Arbiter

**A deterministic referee for LLM-driven C/C++ development.** Arbiter turns the
loop an AI coding agent runs inside a large gtest-guarded codebase — plan, build,
orient, edit, test, verify — into a refereed match: every success claim is
adjudicated from typed, machine-checkable evidence by a Go referee. The model has
no "declare success" interface.

```
plan (playbook) → gear up (build = index) → orient (AST facts) → dispatch
(executor subagents) → edit → build/test → verify (typed predicates) → learn
```

## Why

LLM agents are good at writing code and bad at honestly judging whether it works.
Arbiter removes the judgment from the model entirely:

- **Typed verdicts, never prose.** A task or goal passes only when a typed
  predicate (`shell` / `mcp` / `run` / `fact`) produces evidence whose fields
  match the declared `expect` clauses. A failing gtest run cannot checkmate.
- **Constructive RBAC.** Each agent role (player, curator, executor) talks to its
  own MCP server exposing only that seat's tools. Capability boundaries are
  enforced by construction, not by prompt.
- **Compile done ⇒ index done.** The `arbiter cc` compiler shim journals every
  translation unit during the build; when the build is green, the typed-AST facts
  snapshot is already published. There is no separate indexing step to forget.
- **Anti-false-checkmate machinery.** Recipe pinning, named verification
  predicates, round-sequence guards, and census-digest memoization make it hard
  for an adversarial (or merely optimistic) agent to win without doing the work.

## Quick start

Requirements: Go 1.25+, Python ≥ 3.9 (the engine has zero dependencies), Linux
or macOS, and [Claude Code](https://claude.com/claude-code) for the agent loop.

The repository is self-contained: Go dependencies are vendored under `vendor/`
and the Python engine is embedded in the binary, so both the build and the
deployment work **fully offline**.

```sh
# 1. Build the binary (no network needed — deps are vendored)
git clone https://github.com/HoldThatThgt/arbiter && cd arbiter
make build                                   # produces ./arbiter

# 2. In your C/C++ repository: deploy with the embedded engine (no pip needed)
arbiter init --embedded-engine --openings
```

Then, inside Claude Code in that repository:

| When | Verb |
|---|---|
| once per repo (shell) | `arbiter init` |
| once per repo (Claude Code) | `/arbiter-intro` — probe the build, prove recipes, first gear-up |
| every request | `/arbiter-play <request>` — a refereed match against your request |
| capture knowledge | `/playbook-create` — turn a session into a reusable opening |

Everything else is stock Claude Code: no index commands, no seat management, no
recipe ceremony. See the **[User Guide](docs/user-guide.md)** for the full
walkthrough.

## How it works

Two artifacts, one seam:

- **`arbiter`** (Go, single static binary, vendored deps): the deterministic
  referee and match store, per-seat MCP servers, the deploy/adopt installers, the
  Stop-hook gate, and the `arbiter cc` per-TU compiler shim.
- **`arbiter-engine`** (Python ≥ 3.9, stdlib-only): typed-AST fact extraction and
  search (`facts/`), proven build/test recipes with a gtest-first harness adapter
  (`runs/`), and the build-driven indexing pipeline (`shared/`).

They speak line-delimited JSON-RPC over stdio, contract-tested by golden
transcripts replayed from both runtimes. All state is repo-local under
`.arbiter/`; the only committed knowledge is `playbook/*.md`, `recipes.yaml`,
and `config.yml`. No daemons, no network.

## CLI

```
arbiter init [--embedded-engine] [--openings] [--no-executor] [--remove]
arbiter adopt                 # migrate a legacy chess/crun/cipher deployment
arbiter status [--json]       # compose-on-read deployment & match status
arbiter report [--json] [id]  # journal + run evidence for a match
arbiter serve <seat>          # player | curator | executor MCP server (stdio)
arbiter hook stop             # Claude Code Stop-hook gate
arbiter cc -- <compiler> ...  # compile interposer (installed automatically)
```

## Documentation

- **[User Guide](docs/user-guide.md)** — installation, the four verbs, recipes,
  playbooks, predicates, configuration, troubleshooting.
- [`docs/design.md`](docs/design.md) — the design document of record.
- [`docs/modules/`](docs/modules/) — per-module specifications.
- [`docs/decisions.md`](docs/decisions.md) — the ADR log.

## Development

```sh
make build        # go build ./cmd/arbiter (uses vendor/ automatically)
make test         # go vet + go test -race ./... + Python engine suite
make fmt-check    # gofmt gate
make transcripts  # regenerate the golden JSON-RPC transcript corpus
```

The wire contract between the Go referee and the Python engine is pinned by the
transcripts under `testdata/transcripts/`; any change to a tool schema or
JSON-RPC shape must regenerate them in the same change.
