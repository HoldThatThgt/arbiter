# Module design index

Binding per-module specs. On conflict with `../design.md`, the module doc wins for its module
and the conflict is raised as `needs-decision`. Every module doc follows the same skeleton:
Identity / Inherits / Public surface / Design / Invariants / Tests / Done.

## Go binary (`arbiter`)

| Doc | Module | One line |
|---|---|---|
| [go-referee.md](go-referee.md) | `internal/{match,verify,playbook,journal}` | the deterministic referee: state machine, typed predicate evaluation, journal |
| [go-seat.md](go-seat.md) | `internal/seat` | per-seat MCP servers, constructive RBAC, engine-child lifecycle |
| [go-engineclient.md](go-engineclient.md) | `internal/engineclient` | minimal JSON-RPC stdio client + golden-transcript contract tests |
| [go-interpose.md](go-interpose.md) | `internal/interpose` (`arbiter cc`) | per-TU compiler shim: journal, enqueue, exec-through |
| [go-deploy.md](go-deploy.md) | `internal/deploy` | `arbiter init` / `arbiter adopt`: the one deployment |
| [go-cli.md](go-cli.md) | `cmd/arbiter` | subcommands, status composition, report |

## Python engine (`arbiter-engine`)

| Doc | Module | One line |
|---|---|---|
| [engine-core.md](engine-core.md) | `engine/arbiter_engine/{rpc,config,log}` | stdlib JSON-RPC loop, namespaces, config, errors, logging |
| [engine-facts.md](engine-facts.md) | `engine/arbiter_engine/facts/` | cipher-2 absorbed: extraction, snapshots, overlay, search/detail, extract-cache |
| [engine-runs.md](engine-runs.md) | `engine/arbiter_engine/runs/` | recipes, gtest-first adapters, build cache, proven lifecycle, guidance |
| [engine-shared.md](engine-shared.md) | `engine/arbiter_engine/shared/` | census, lock inventory, compile-db journal, build-driven indexing pipeline |

## Product surface

| Doc | Module | One line |
|---|---|---|
| [skills-and-playbooks.md](skills-and-playbooks.md) | `.claude/skills/*`, openings | the four verbs, freeplay, gear-up convention, intro bootstrap |
