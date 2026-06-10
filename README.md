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

## The user contract — four verbs

| When | Verb |
|---|---|
| once per repo (shell) | `arbiter init` |
| once per repo (in Claude Code) | `/arbiter-intro` |
| every request | `/arbiter-play <request>` |
| capture knowledge | `/playbook-create` (where skill-create used to be) |

Beyond these, the session is stock Claude Code: no recipe ceremony, no index commands, no seat management.

## Architecture in one paragraph

Two artifacts, one seam. **`arbiter`** (Go, single static binary, vendored deps): referee, seats
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

## Repository map

```
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
