# Arbiter

One referee-adjudicated dev loop for gtest-guarded C DBMS codebases — the unification of
[cipher-2](https://github.com/HoldThatThgt/cipher-2) (libclang FACT engine),
[chess](https://github.com/HoldThatThgt/chess) (deterministic playbook referee), and
[crun-mcp](https://github.com/HoldThatThgt/crun-mcp) (proven build/test recipes) into a single product.

The unit of value is the loop an LLM runs when developing C in a large codebase:
**plan** (playbook) → **gear up** (build with the request's profile — which *is* the index) →
**orient** (AST facts) → **dispatch** (executor subagents) → **edit** → **build/test** →
**verify** (machine-checkable typed predicates) → **learn** (gotchas, proven recipes).
A deterministic Go referee owns every transition; the model has no "declare success" interface.

## Quick start

```sh
# 1. install — one command, one artifact (engine + gdb-mcp + perf-mcp embedded)
git clone https://github.com/HoldThatThgt/arbiter && cd arbiter && make install

# 2. wire your C/C++ repo — one command (idempotent, seconds)
cd /path/to/your/c/repo && arbiter init

# 3. play
claude
#   then inside the session:   /arbiter-play <your request>
#   capture knowledge:         /playbook-create
```

Everything ships in the one binary; the only system prerequisite is `python3` (≥ 3.9) — if it
is missing, `arbiter init` says so and that is the only thing you will ever be asked to
install. `arbiter --help` and `arbiter init --help` state exactly what each command does.

Your repo keeps building with **its own compiler** (gcc/g++ of any version) — arbiter never
swaps it. Only the facts index needs LLVM Clang ≥ 16 (or Apple Clang ≥ 15) for its own AST
extraction, isolated from your build toolchain; without it you lose facts, never builds. Live
debugging additionally wants a working host `gdb` (`python3 -m arbiter_engine.gdbmcp doctor
--root .` tells you).

Details, predicates, playbook grammar, troubleshooting: **[docs/user-guide.md](docs/user-guide.md)**.

## The user contract — four verbs

| When | Verb |
|---|---|
| once per repo (shell) | `arbiter init` |
| once per repo (in Claude Code) | `/arbiter-intro` *(lands with M7)* |
| every request | `/arbiter-play <request>` |
| capture knowledge | `/playbook-create` (where skill-create used to be) |

Beyond these, the session is stock Claude Code: no recipe ceremony, no index commands, no seat management.

## Architecture in one paragraph

Two runtimes, one seam, one delivered artifact (ADR-0011). **`arbiter`** (Go, single static binary, vendored deps): referee, seats
(constructive RBAC via per-seat MCP servers), Stop-hook gate, deploy, and the `arbiter cc` per-TU
compiler shim. **`arbiter-engine`** (Python ≥3.9, stdlib-only, pip-installed): the `facts/` namespace
(cipher-2 absorbed verbatim — typed-AST extraction, snapshots, `search`/`detail`), the `runs/`
namespace (recipes, gtest-first harness adapters, census-validated build cache), and `shared/`
(work-tree census, lock inventory, build-driven indexing pipeline). They speak line-delimited
JSON-RPC over stdio, contract-tested by golden transcripts. All state lives in repo-local
`.arbiter/`; committed knowledge is `playbook/*.md` + `recipes.yaml` + `config.yml`.

The keystone mechanism: chess's 1-bit predicates (exit code / `isError`) become **typed evidence
claims** — `{kind:"run", expect:{overall:"passed"}}`, `{kind:"fact", expect:{complete:true,
max_results:1}}` — evaluated by the two native engines and compared field-by-field by the referee.
A failing gtest run can no longer checkmate. The facts index has **no standalone lifecycle**: the
`arbiter cc` shim journals every compiler invocation and extraction overlaps the build, so when the
gear-up build is green the snapshot is published. Compile done ⇒ index done.

## Bundled diagnostics — gdb-mcp & perf-mcp

`arbiter-engine` ships two diagnostic MCP servers (ADR-0010): **gdb-mcp** (structured GDB
debugging — sessions, watchpoints, stack/locals snapshots) and **perf-mcp** (C perf triage —
ranked findings, argv-only before/after measurement). `arbiter init` wires both into
`.mcp.json` and writes the `arbiter-debugger` executor agent whenever it can resolve the
engine; predicates adjudicate their structured fields, never text. Live debugging additionally
needs a working host `gdb` (`doctor` tells you). Full detail:
[user-guide §6–7](docs/user-guide.md#6-bundled-diagnostics-in-depth).

## Repository map

```
docs/user-guide.md      the user manual: setup, playbooks, predicates, diagnostics, troubleshooting
docs/design.md          master design document (the spec of record)
docs/modules/           per-module elaborated designs (binding specs for implementation)
docs/decisions.md       ADR log — owner-signed decisions; specs change ONLY through this file
docs/migration.md       milestone plan (M0–M8), each independently green
PROCESS.md              how this repo is built: GPT implements, Claude reviews, owner adjudicates
prompts/gpt-implementer.md   the standing prompt for the implementer agent
```

## Status

Pre-implementation. The design is complete (`docs/design.md`); implementation proceeds
issue-by-issue per `docs/migration.md` under the process in `PROCESS.md`.
