# Arbiter User Guide

This guide covers installing Arbiter, deploying it into a C/C++ repository, and
running refereed development matches from Claude Code.

- [1. Installation](#1-installation)
- [2. Deploying into a repository](#2-deploying-into-a-repository)
- [3. The four verbs](#3-the-four-verbs)
- [4. Recipes](#4-recipes)
- [5. Playbooks and openings](#5-playbooks-and-openings)
- [6. Verification predicates](#6-verification-predicates)
- [7. Facts: the build-driven index](#7-facts-the-build-driven-index)
- [8. Configuration reference](#8-configuration-reference)
- [9. CLI reference](#9-cli-reference)
- [10. Runtime layout](#10-runtime-layout)
- [11. Troubleshooting](#11-troubleshooting)

## 1. Installation

Requirements:

- Go 1.25+ (build only)
- Python ≥ 3.9 on the target machine (the engine is pure stdlib — zero
  dependencies)
- Linux or macOS; a local filesystem (Arbiter refuses to deploy onto NFS/SMB)
- [Claude Code](https://claude.com/claude-code) for the agent loop

Build the binary:

```sh
git clone https://github.com/HoldThatThgt/arbiter && cd arbiter
make build          # → ./arbiter ; put it anywhere on your machine
```

**Offline installation is fully supported.** Go dependencies are vendored under
`vendor/` (the build never touches the network), and the Python engine is
embedded inside the binary, so a single `arbiter` file is everything a target
machine needs. The binary does not have to be on `PATH` — every wiring Arbiter
writes uses absolute paths.

The engine can run in either of two modes:

| Mode | How | When |
|---|---|---|
| **Embedded** (recommended) | `arbiter init --embedded-engine` unpacks a digest-verified copy of the engine into `.arbiter/engine` | No pip, no network, hermetic per-repo |
| Installed | `pip install ./engine` (from this repo — works offline) so `python -m arbiter_engine` resolves | Shared engine across repos |

## 2. Deploying into a repository

In the root of the C/C++ repository you want to develop in:

```sh
arbiter init --embedded-engine --openings
```

This is a **merge, not an overwrite** — pre-existing settings, MCP servers,
hooks, and `.gitignore` entries are preserved. It writes:

- `.mcp.json` — per-seat MCP servers (`arbiter serve player|curator|executor`)
  wired with absolute paths and a per-repo seat key
- `.claude/agents/` — the curator and executor subagent definitions
- `.claude/skills/` — the `/arbiter-play`, `/arbiter-intro`, and
  `/playbook-create` skills
- `.claude/settings.json` — deny rules protecting referee state, plus the Stop
  hook (`arbiter hook stop`)
- `.arbiter/config.yml`, `.arbiter/recipes.yaml`, `.arbiter/playbook/` — the
  three things you commit; with `--openings`, a base playbook library
- `.arbiter/run/engines.json` — the verified engine record

Flags: `--no-executor` skips the executor seat; `--remove` round-trips the
deployment out again without touching anything you authored. `arbiter init` is
idempotent — re-run it after moving or rebuilding the binary.

Migrating from a legacy `chess` / `crun-mcp` / `cipher-2` deployment:

```sh
arbiter adopt    # migrates .cipher/config.yml etc.; preserves the legacy
                 # config as comments for manual review; derived state is
                 # deleted by contract, never migrated
```

## 3. The four verbs

| When | Verb |
|---|---|
| once per repo (shell) | `arbiter init` |
| once per repo (Claude Code) | `/arbiter-intro` |
| every request | `/arbiter-play <request>` |
| capture knowledge | `/playbook-create` |

**`/arbiter-intro`** bootstraps the repo under adjudication: probes the build
system, derives and *proves* recipes (a recipe is only "proven" after a real
green run), installs the compile shim, scans for instrumentation macros
(recommending — never auto-writing — `key_flags`), runs the first gear-up, and
deploys the base openings. Its checkmate is typed: proven-recipe count plus a
published facts snapshot.

**`/arbiter-play <request>`** runs a refereed match. The player loads an opening
(or freeplay), then loops: `ShowStepJob` → create tasks → executor subagents do
the work and `SubmitTask` with a typed result predicate → the referee verifies
and adjudicates → `CheckStepJob` advances the round. If the playbook declares a
`[SetGoal]`, every successful round adjudication also runs the goal predicate —
pass means checkmate and the match finishes successfully at once. Reaching `END`
with the goal still failing finishes the match as a failure. The model never
gets to say "done".

**`/playbook-create`** turns what a session learned into a committed opening
under `.arbiter/playbook/`, validated against the referee grammar at creation
time.

Progress is observable from the shell at any time:

```sh
arbiter status            # deployment, engine, match, runs — composed on read
arbiter report <match>    # journal + run evidence for a finished match
```

## 4. Recipes

`.arbiter/recipes.yaml` (RecipeBook v2) is the committed catalog of proven
build/test commands. `targets:` is a **sequence**; `profiles:` overlay
environment/flags:

```yaml
# Arbiter RecipeBook v2.
profiles:
  asan:
    cflags_append: [-fsanitize=address]
targets:
  - id: unit
    harness: gtest
    sources: ["src/**/*.c", "include/**/*.h"]
    stages:
      src_compile:
        cmd: make -j
      test_run:
        cmd: ./build/unit_tests
        timeout_s: 600
```

Rules worth knowing:

- Target ids are path-safe identifiers (`[A-Za-z0-9._-]`, no leading dot).
- Compile stages run with `CC`/`CXX` wrapped by `arbiter cc` automatically —
  that is how the facts index gets built as a side effect of your build.
- The gtest harness injects `--gtest_output` XML and parses **only** the result
  file; per-test outcomes become typed evidence.
- **Recipe pinning:** when a playbook is loaded, the recipe book is pinned into
  match state. Editing `recipes.yaml` mid-match makes run predicates fail with
  `recipe_pin_mismatch` — finish or reload the match instead.

## 5. Playbooks and openings

Playbooks are markdown files under `.arbiter/playbook/` with YAML frontmatter
and bracket-token sections. The grammar reference deployed into every repo is
`.arbiter/playbook/FORMAT.md`. Skeleton:

```markdown
---
name: hotfix-verify
description: Fix the build failure and verify the regression.
max_steps: 32
verify_policy: named
---

[Verify] suite-green
run: unit
tests: ["*"]
expect: {"overall":"passed","max_failed":0}
allow_overrides: ["tests"]

[SetGoal]
verify: suite-green

[STEP] diagnose
[StepJob]
Find the direct cause of the failure. Do not edit code.
[CheckList]
- Root cause stated with evidence file paths
[Branch]
success: fix
failure: diagnose
...
```

- **`[Verify] <name>`** sections declare curated predicates the executor invokes
  by name (`SubmitTask` with `{"result": {"verify": "suite-green"}}`). They are
  snapshotted into match state at load — editing the playbook mid-match cannot
  swap a predicate. Curated specs are closed; `allow_overrides` opts only
  `tests`/`options` open for the submitter.
- **`verify_policy: named`** forces every task verdict through a curated
  predicate; the default `open` also allows inline specs.
- **`[SetGoal]`** declares the checkmate predicate, inline or as
  `verify: <name>`.
- Comments: a line starting with `#` inside `[SetGoal]`/`[Verify]` is a comment.
  Inline `#` comments are not supported and fail loudly where the field grammar
  excludes them; `shell:` values run verbatim to end of line.
- **`[Gotcha]`** sections accumulate reusable caveats — the player appends them
  at run time via `NotePlaybook`.

The base opening library (installed by `--openings`): **freeplay** (open
predicates, general work), **gold-digger** (prove the repro fails → fix → prove
it passes), **regression-triage**, **recipe-derivation**, **review** (bug-hunt
that proves one bug with a failing test plus reachability facts), **feature**
(user scenarios → approved red tests → green by any means → elevate into the
rightful module), and **debug** (pin a deterministic repro test before any fix).

## 6. Verification predicates

Every task submission and goal is a typed predicate. The referee compares
evidence field-by-field against `expect`; free-form output never influences a
verdict.

| Kind | Shape | Passes when |
|---|---|---|
| `shell` | `shell: <command>` (+ `timeout_s`, `output_lines`) | exit code 0 |
| `mcp` | `mcp: <server> <tool>` + `arguments: {...}` + `expect: [...]` | every clause holds |
| `run` | `run: <recipe>` + `tests: [...]` + `expect: {...}` | every clause holds |
| `fact` | `fact: <query>` + `expect: {...}` | every clause holds |

`run` expect clauses: `overall` (`"passed"` / `"failed"` or `{"one_of": [...]}`),
`max_failed`, `min_passed`, `test: {"name": "Suite.Case", "result": "passed"}`,
`facts: {"published": true|false}`.

`fact` expect clauses: `min_results`, `max_results`, `complete`, `reachable`,
`total_at_least`.

`mcp` expect is an array of at most 8 clauses
`{"path": "result.field", "op": "eq|ne|ge|le|exists", "value": <scalar>}` —
closed operator set, scalar operands, no wildcards. A predicate targeting the
arbiter binary itself is rejected (`reserved_server`) — the referee cannot be
asked to interrogate itself.

Empty expects fail closed: a predicate with no clauses can never pass.

## 7. Facts: the build-driven index

There is no "build the index" command. During any compile stage, `arbiter cc`
journals each translation unit; after a green build the engine consumes the
journal, extracts typed AST facts (functions, fields, relations) with a
content-addressed cache, and publishes a snapshot under
`.arbiter/facts/snapshots/current`. **Compile done ⇒ index done.**

Agents query it through the seat tools `search` (multi-term AND plus relation
predicates) and `detail`. Fact-kind predicates make index queries part of
adjudication — e.g. "this function exists and is reachable" as a typed claim
with `snapshot_id` evidence.

Cache keys include the TU content, a repo-wide headers digest, the toolchain,
and semantic flags — `-fsanitize=*` always keys (a sanitizer build never reuses
plain-build facts); `-O`/`-g` are ignored unless listed in
`facts.index_on_build.key_flags`.

## 8. Configuration reference

`.arbiter/config.yml`:

```yaml
facts:
  index_on_build:
    pool: 4            # extraction worker cap during the build tail
    key_flags: []      # extra compile flags that should key the facts cache
match:
  goal_memo: false     # memoize goal passes per workspace digest (default off)
```

Environment variables:

| Variable | Effect |
|---|---|
| `ARBITER_ENGINE_PYTHON` | interpreter used for the engine (then `PYTHON`, then `python3`) |
| `ARBITER_ENGINE_CALL_TIMEOUT_S` | engine call deadline when the caller has none (default 600) |

## 9. CLI reference

```
arbiter init [--embedded-engine] [--openings] [--no-executor] [--remove]
arbiter adopt
arbiter status [--json]
arbiter report [--json] [match_id]
arbiter serve player|curator|executor
arbiter hook stop
arbiter cc -- <real-compiler> [args...]
```

`serve` speaks MCP over stdio and exits on EOF — it is always spawned by Claude
Code via `.mcp.json`, never run as a daemon. `cc` is fail-open: it never breaks
a build, even when journaling fails (the miss is recorded and facts publication
is withheld instead). `hook stop` is the only other component allowed to fail
open.

## 10. Runtime layout

```
.arbiter/
  config.yml          committed — engine/match configuration
  recipes.yaml        committed — proven recipe book
  playbook/*.md       committed — openings + FORMAT.md grammar reference
  engine/             embedded engine (digest-verified; gitignored)
  match/              match state, journal, seat key (gitignored)
  facts/              compile journal, extract cache, snapshots (gitignored)
  runs/               async run state (sqlite; gitignored)
  run/engines.json    engine verification record (gitignored)
```

Commit `config.yml`, `recipes.yaml`, and `playbook/`. Everything else under
`.arbiter/` is runtime state and is gitignored by `init`.

## 11. Troubleshooting

**`arbiter-engine verification failed` during init** — the engine isn't
resolvable. Either deploy hermetically (`arbiter init --embedded-engine`) or
install it (`pip install ./engine` from the Arbiter repo, offline-capable), and
ensure the right interpreter wins via `ARBITER_ENGINE_PYTHON`.

**`arbiter init refused network filesystem`** — the runtime relies on POSIX
file locks; deploy on a local filesystem.

**`recipe_pin_mismatch`** — `recipes.yaml` changed while a match was active.
Finish the match or load the playbook again to re-pin.

**`lock_timeout`** — another seat holds a lock past its deadline (default
bounds are generous). Typically a stale crashed process: re-running the
operation after it exits recovers; locks are advisory flocks, nothing to clean.

**Engine call timed out / `engine_unavailable`** — a synchronous engine call
exceeded the deadline. Raise `ARBITER_ENGINE_CALL_TIMEOUT_S` for legitimately
long recipes, or move long runs to async goals (the `[SetGoal] run:` path),
which poll without a wall-clock cap. Seats respawn a poisoned engine
automatically on the next call.

**`verify_not_found` / `verify_policy` on SubmitTask** — the playbook requires
named predicates; `ShowStepJob` lists the available names.

**Match seems stuck on `goal_running`** — the async run is still executing;
`arbiter status` shows the live run state. Runs are bounded by the spec
`timeout_s` and a worker-liveness check, so a dead worker surfaces as
`worker_lost` rather than hanging the match.
