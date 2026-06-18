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
- [8. Bundled diagnostics: gdb-mcp & perf-mcp](#8-bundled-diagnostics--gdb-mcp--perf-mcp)
- [9. Configuration reference](#9-configuration-reference)
- [10. CLI reference](#10-cli-reference)
- [11. Runtime layout](#11-runtime-layout)
- [12. Troubleshooting](#12-troubleshooting)

**Glossary** — terms used throughout this guide:

- **seat** — an MCP-scoped agent role (player, curator, executor).
- **gear-up** — the build-as-index step: a green build that publishes a facts
  snapshot as a side effect.
- **opening** — a selectable playbook.
- **checkmate** — the typed goal-pass that ends a match.
- **predicate** — the typed pass/fail check the referee evaluates.
- **recipe** — a proven build/test command.

## 1. Installation

Requirements:

- Go 1.25+ (build only)
- Python ≥ 3.9 on the target machine (the engine is pure stdlib — zero
  dependencies)
- LLVM Clang ≥ 16 / Apple Clang ≥ 15 (facts index only; builds and matches
  work without it)
- Linux or macOS; a local filesystem (Arbiter refuses to deploy onto NFS/SMB)
- [Claude Code](https://claude.com/claude-code) for the agent loop

Install — one command, one artifact (ADR-0011):

```sh
git clone https://github.com/HoldThatThgt/arbiter && cd arbiter
make install        # root → /usr/local/bin ; others → $HOME/.local/bin (override with PREFIX=…)
```

If `arbiter` is not found afterwards, the install prefix is not on your PATH —
the install prints the exact note; add the printed `bin` directory or rerun
with `PREFIX=/usr/local`.

**Offline installation is fully supported.** Go dependencies are vendored under
`vendor/` (the build never touches the network), and the Python engine —
including the bundled `gdb-mcp` and `perf-mcp` diagnostic servers — is embedded
inside the binary, so a single `arbiter` file is everything a target machine
needs. The binary does not have to be on `PATH` — every wiring Arbiter writes
uses absolute paths.

**How the engine resolves** (automatic, per repo, at `arbiter init`):

1. an **installed** `arbiter-engine` package for `python3` is preferred when
   present (`pip install ./engine` from this repo — optional, for sharing one
   engine across repos);
2. otherwise init **materializes the embedded engine** into repo-local
   `.arbiter/engine/` — digest-verified on every spawn, gitignored, protected
   by Edit/Write deny rules; `--embedded-engine` forces this mode even when a
   package is installed;
3. only when `python3` itself is missing does init fail — with the fix in the
   error. Installing python3 (≥ 3.9) is the only setup you will ever be asked
   to do.

Upgrading arbiter = reinstall the binary, re-run `arbiter init` in each repo
(idempotent; it refreshes the embedded engine and the wiring).

## 2. Deploying into a repository

In the root of the C/C++ repository you want to develop in:

```sh
arbiter init
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
  three things you commit; the shipped openings are *refreshed to the shipped
  version on every init* (so upgrading arbiter delivers the latest openings).
  To customize, fork an opening to a new name — your own-named books, added via
  `AddPlayBook`, are never touched. `config.yml` and `recipes.yaml` themselves
  stay write-if-missing.
- `.mcp.json` entries for the bundled **gdb-mcp** and **perf-mcp** diagnostic
  servers (launched via the engine interpreter; existing entries are preserved)
- `.claude/agents/arbiter-debugger.md` — the diagnose-and-fix executor agent
  wired with both diagnostic servers
- `.arbiter/run/engines.json` — the verified engine record

Flags: `--no-executor` skips the executor seat; `--remove` round-trips the
deployment out again without touching anything you authored. `arbiter init` is
idempotent — re-run it after moving or rebuilding the binary.

**Then open (or restart) Claude Code in this repository** so it loads the newly
written skills (`.claude/skills/`) and MCP servers (`.mcp.json`) — the slash
commands and the player/curator/executor servers appear only after the session
picks them up. Do this before running `/arbiter-intro`.

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
    harness:
      kind: gtest
    sources: ["src/**/*.c", "include/**/*.h"]
    src_compile:
      cmd: [make, -j]
    test_run:
      cmd: [./build/unit_tests]
      timeout_s: 600
```

Rules worth knowing:

- Target ids are path-safe identifiers (`[A-Za-z0-9._-]`, no leading dot).
- Each target lists its stages **directly** as `src_compile` / `test_compile` /
  `test_run` keys (there is no `stages:` wrapper), and `harness` is a mapping
  (`harness:` then `kind: gtest`), not a bare scalar. A stage's `cmd` is an argv
  list run without a shell — write `[make, -j]`, not `make -j`.
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
- **`[Submit] <name>`** inside a `[STEP]` binds that step to a curated `[Verify]`
  predicate: the executor must finish the dispatched task with exactly
  `{"verify": "<name>"}` and cannot weaken or substitute it. A step carries either
  tasks or a `[Checkpoint]`, never both.
- **`[Checkpoint]`** is a human-confirmation gate: instead of dispatching work, the
  player puts the step's question to *you* and relays your pass/fail decision via the
  `SubmitCheckpoint` tool (pass advances the round, fail loops the step). The model
  cannot self-approve a checkpoint.
- Comments: a line starting with `#` inside `[SetGoal]`/`[Verify]` is a comment.
  Inline `#` comments are not supported and fail loudly where the field grammar
  excludes them; `shell:` values run verbatim to end of line.
- **`[Gotcha]`** sections accumulate reusable caveats — the player appends them
  at run time via `NotePlaybook`.
- **`capabilities:`** (frontmatter, optional) capability-gates the
  recipe-authoring tools (`register`, `import_recipes`, `scan`) — those tools are
  live only while a playbook declaring the capability is loaded. The ONLY legal
  value is `recipes`; any unknown or duplicate value is a hard parse error.

The opening library is delivered by `arbiter init`, which refreshes each shipped
opening to its shipped version on every init (your own-named books, added via
`AddPlayBook`, are never touched — fork to a new name to customize). Four
starter openings are named by USER INTENT (ADR-0012 — imperative, verb-first,
kebab-case, ≤3 segments; descriptions lead "Use when …" and cross-point
"Do not use … (use <other>)" so the curator deduplicates at selection time):

| Opening | Use when | The referee mechanism inside |
|---|---|---|
| `fix-reported-bug` | a known crash/misbehavior must die | two plain `src_compile` run predicates: repro-runs-red (expect overall failed) and suite-green (expect overall passed, max_failed 0), plus RegisterTest-freeze untouchability |
| `hunt-latent-bugs` | find defects nobody pinned down | symptom-test polarity: the test passes iff the bug exists, so `build && run` exit 0 is a machine proof |
| `build-feature` | new functionality, scenario-first | `build && ! run` proves tests red for the right reason; test untouchability rides every later predicate |
| `fix-slow-path` | something is measurably slow | a frozen complexity-ratio test (time(2N)/time(N) under a fixed bound K, robust to host speed), gated by the two plain run predicates; perf-mcp's noise-band/second-baseline measurement is analysis-only, not the verdict |

The `build-feature` and `hunt-latent-bugs` mechanisms rely on **test untouchability**:
the test-author executor calls `RegisterTest {"paths": [...]}` to freeze the test
file(s), and every later run predicate re-hashes them at worker time — a "fix" that
secretly weakens the frozen test is rejected, not rewarded.

Alongside them ship the design-canonical intro openings: **freeplay** (open
predicates, general work), **gold-digger** (prove the repro fails → fix →
prove it passes, on typed run/fact predicates), **regression-triage**, and
**recipe-derivation** (capability-gated recipe authoring). The full naming and
predicate-discipline rules are in your repo's `.arbiter/playbook/FORMAT.md`
after init, and `/playbook-create` enforces them on new openings.

## 6. Verification predicates

Every task submission and goal is a typed predicate. The referee compares
evidence field-by-field against `expect`; free-form output never influences a
verdict.

| Kind | Shape | Passes when |
|---|---|---|
| `shell` | `shell: <command>` (+ `timeout_s`, `output_lines`) | exit code 0 |
| `mcp` | `mcp: <server> <tool>` + `arguments: {...}` + `expect: [...]` | every clause holds |
| `run` | `run: <recipe>` + `tests: [...]` (+ `options: {...}`) + `expect: {...}` | every clause holds |
| `fact` | `fact: <query>` + `expect: {...}` | every clause holds |

`run` expect clauses: `overall` (`"passed"` / `"failed"` or `{"one_of": [...]}`),
`max_failed`, `min_passed`, `test: {"name": "Suite.Case", "result": "passed"}`,
`facts: {"published": true|false}`.

`fact` expect clauses: `min_results`, `max_results`, `complete`, `reachable`,
`total_at_least`.

`mcp` expect is an array of at most 8 clauses
`{"path": "summary.all_successful", "op": "eq|ne|ge|le|exists", "value": <scalar>}` —
closed operator set, scalar operands, no wildcards. Paths are dot-separated and
rooted at the tool's `structuredContent` (object keys and array indices, e.g.
`checks.0.ok`); the response envelope (`isError`, `content`) is not addressable.
An errored call (`isError=true`) fails the verdict automatically, even when
every clause matches — and missing paths or type mismatches fail their clause,
including `ne`. Example against the bundled perf server:

```json
{"kind": "mcp", "server": "perf-mcp", "tool": "perf.measure_command",
 "arguments": {"command": ["./bench"], "repeat": 5},
 "expect": [{"path": "summary.all_successful", "op": "eq", "value": true},
            {"path": "summary.median_wall_seconds", "op": "le", "value": 2.5}]}
```

A predicate targeting the
arbiter binary itself is rejected (`reserved_server`) — the referee cannot be
asked to interrogate itself; the bundled diagnostic servers run via the engine
interpreter, so they are valid targets.

Empty expects fail closed: a `run`, `fact`, or `mcp` predicate whose `expect` is
present but empty can never pass. The one exception is an `mcp` predicate that omits
`expect` entirely — with no fields to compare it passes whenever the call returns
without `isError` (the legacy "did it run cleanly" check). Give every `mcp` predicate
an `expect` clause whenever the verdict should depend on a field, not just on the call
succeeding.

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

**Two toolchains, isolated** (inherited verbatim from cipher-2): your repo
builds with whatever it needs — gcc/g++ of any vintage is the normal case —
and arbiter never replaces or version-gates it; `arbiter cc` only journals and
execs your compiler bit-exact. Extraction parses the journaled TUs with its
*own* Clang + libclang (**LLVM Clang ≥ 16 / Apple Clang ≥ 15**, located
automatically, capability-probed) after cleaning gcc-only flags out of the
recorded commands; the AST path never requires GCC. No capable Clang on the
machine ⇒ no facts index (a typed failure on the gear-up verdict) — builds,
matches, shell/mcp predicates, and the bundled diagnostics keep working.

## 8. Bundled diagnostics — gdb-mcp & perf-mcp

The engine ships two diagnostic MCP servers (ADR-0010); init wires both into
`.mcp.json` and writes the `arbiter-debugger` executor agent that uses them.
They are FOREIGN servers in the predicate sense — launched via the engine
interpreter, never via the arbiter binary — so mcp-kind `expect` predicates
adjudicate their structured fields.

**gdb-mcp** (`python3 -m arbiter_engine.gdbmcp`) — structured GDB/MI debugging,
typed JSON in `structuredContent`, never scraped terminal text: `gdb_start`
(exec/core; attach/remote are opt-in serve flags), `gdb_exec`, `gdb_breakpoint`
(including watchpoints — `kind: watch` is the memory-corruption workhorse, with
`rwatch`/`awatch` for read/access watches and a `hardware: true` flag to force a
hardware breakpoint),
`gdb_select`, `gdb_stack`, `gdb_snapshot` (stop reason + threads + stack +
locals + registers in one call), `gdb_eval`, `gdb_memory` (bounded reads),
`gdb_command` (guarded console — `shell`/`python`/`source`/… denied unless the
server runs with `--allow-dangerous-commands`), `gdb_sessions`, `gdb_stop`,
`gdb_diagnostics`. Session state and a redacted audit log live in `.gdb-mcp/`;
serving with `--no-audit` disables the `.gdb-mcp/audit.jsonl` log.

`gdb-mcp` wraps the **host** `gdb`, which remains a system prerequisite for
live debugging. Check readiness — the probe compiles a one-liner and verifies
GDB can actually run it:

```sh
PYTHONPATH=.arbiter/engine python3 -m arbiter_engine.gdbmcp doctor --root .
```

The `PYTHONPATH=.arbiter/engine` prefix is needed for the default
embedded-engine layout (`.arbiter/engine` is not otherwise on `PYTHONPATH`, so
the bare `python3 -m arbiter_engine.gdbmcp …` fails with `ModuleNotFoundError`).
The bare form works once you have installed the package (`pip install ./engine`).

On macOS, Homebrew GDB commonly parses symbols but cannot launch local
inferiors (`gdb_run: Don't know how to run`) — codesign gdb, use a remote
target, or do live debugging on Linux; everything else keeps working. Build
debug targets with `-g -gdwarf-4 -O0`. The typed guidance codes
(`darwin_gdb_codesign_required`, `debug_info_format_unsupported`) are emitted by
the live `gdb_start`/`gdb_exec` path when it classifies these failures — `doctor`
itself reports only plain-text checks, surfacing the same condition as a failed
`gdb_run` check with a plain-text detail.

**perf-mcp** (`python3 -m arbiter_engine.perfmcp`) — C performance triage:
`perf.scan_c` (ranked findings with stable rule ids — `C.PERF.ALLOC_IN_LOOP`,
`C.PERF.STRLEN_IN_LOOP`, … — file:line evidence, severity/confidence, file and
byte budgets), `perf.explain_finding` (false-positive checks, safe fix
strategy, measurement plan), `perf.measure_command` (argv arrays only — shell
strings rejected; wall/user/system seconds, max RSS, median summary),
`perf.toolchain_probe`. All results are schema-versioned (`perf-mcp.scan.v1`,
…) so `expect` paths stay stable. A scan is **triage, not proof** — the
fix-slow-path opening insists on a measured baseline and a measured gain.

## 9. Configuration reference

`.arbiter/config.yml`:

```yaml
facts:
  index_on_build:
    pool: 4                  # cap extraction workers during the build tail (unset ⇒ CPU-derived)
    key_flags: []            # extra compile flags that should key the facts cache
  incremental:               # live background index between builds (ADR-0018); all knobs optional
    enabled: true            # automatic background incremental index (default on)
    poll_interval_ms: 500    # how often the poll thread scans tracked sources for edits
    debounce_ms: 100         # settle time after an observed edit before re-extracting
    overlay_ttl_seconds: 600 # overlay GC age (0 ⇒ never GC)
    max_dirty_files: 500     # refuse to build an overlay larger than this dirty set
  toolchain:                 # pin the INDEXER's clang/libclang — never the build; all keys optional
    clang: /usr/lib/llvm-16/bin/clang            # which clang the extractor probes (libclang auto-derived from ../lib)
    libclang: /usr/lib/llvm-16/lib/libclang.so   # override only for nonstandard layouts
    clang_args: [--gcc-toolchain=/opt/gcc-7.3.0] # extra parse flags — where a gcc toolchain pin belongs
match:
  goal_memo: false           # memoize goal passes per workspace digest (default off)
```

`pool` is the same knob cipher-2 exposed as `extractor.worker_count`, so `arbiter adopt`
migrates that value straight into it; it is the **single** worker knob — it drives both the
build-tail extraction and incremental dirty re-extraction (there is no separate incremental
`worker_count`). `facts.incremental` is now a **live section** (ADR-0018): the absorbed
cipher-2 engine runs an automatic background index that re-extracts edited sources between
builds and publishes a temporary overlay merged into `search`/`detail` at query time; the
referee forces a synchronous reconcile before every fact predicate so adjudication is never
stale. `facts.extractor` (string) is still **reserved** — arbiter ships a single Clang
extractor, so it parses and validates but selects nothing.

`facts.toolchain` pins the toolchain the **code indexer** uses, scoped to indexing only — it
populates the libclang extractor's config and is never consulted by the build, which keeps
running from the recipe's own commands against the host PATH. Use it when the indexer should
parse with a different clang than `clang` on PATH (e.g. to match a build compiled elsewhere)
without perturbing the build. `clang` selects the probe binary and its sibling libclang is
auto-derived; set `libclang` only when that derivation can't find a matching-major library.
There is deliberately **no gcc binary key**: the indexer never executes gcc (a gcc path would
only change a cache key), so to make indexing read a specific gcc's libstdc++ headers, pass the
clang mechanism — `clang_args: [--gcc-toolchain=/path/to/gcc-install]` (or `--gcc-install-dir=`,
`-isystem`, `--sysroot`).

The code index is a **must-have**: a non-working indexer *toolchain* is a hard stop, not a
degraded run. If clang or libclang is missing, their majors mismatch, or the clang can't emit a
type-driven AST, the failure is unconditional (no opt-out) and surfaces at both points the
toolchain is exercised: the build-tail publish makes the run **error** with `failure:
indexer_unavailable`, and the synchronous reconcile that gates every fact predicate raises the
`indexer_unavailable` error rather than serving a stale index — so a match can never silently
adjudicate without facts. (The background index daemon stays best-effort: a broken toolchain just
skips a tick, so read-only `search`/`detail` over an already-published index keep working.)
`facts.toolchain` is the remedy when the host's default `clang` isn't the one you want indexing to
use. Note the scope: only a broken *toolchain* hard-stops; a recipe with no `compile_db:` section,
a build that failed, or an uncaptured compile journal stay their existing typed not-published
results (there is simply nothing to index). The `runs:` and `engine:` sections must be empty when
present — any sub-key is rejected as unknown.

Environment variables:

| Variable | Effect |
|---|---|
| `ARBITER_ENGINE_PYTHON` | interpreter used for the engine (then `PYTHON`, then `python3`) |
| `ARBITER_ENGINE_CALL_TIMEOUT_S` | engine call deadline when the caller has none (default 600) |
| `ARBITER_ASSUME_FS` | override the filesystem-kind probe at `arbiter init` (e.g. force `local` when the network-mount heuristic misfires and refuses a deploy) |

The remaining `ARBITER_*` variables (seat key, build id, engine role, engine seat) are
wiring that `arbiter init` injects into the seat/companion entries — they are managed for
you, not user-set.

## 10. CLI reference

```
arbiter init [--no-executor] [--remove] [--embedded-engine]
arbiter adopt
arbiter status [--json]
arbiter report [--json] [match_id]
arbiter serve player|curator|executor [--root DIR]
arbiter hook stop|guard|subagent-stop [--root DIR]
arbiter cc [--root DIR] -- <real-compiler> [args...]
```

`serve` speaks MCP over stdio and exits on EOF — it is always spawned by Claude
Code via `.mcp.json`, never run as a daemon. The three `hook` subcommands are all
wired by `arbiter init` (each with an absolute `--root`, ADR-0014) and all fail open
so a broken referee never traps your session: `stop` is the Stop-hook checkmate gate,
`guard` is the PreToolUse path fence over playbook/match/engine/agent files
(ADR-0015), and `subagent-stop` adjudicates an executor subagent's submission. `cc`
is likewise fail-open: it never breaks a build, even when journaling fails (the miss
is recorded and facts publication is withheld instead). Every spawned entry carries an
explicit `--root`; cwd is only a hand-run fallback.

## 11. Runtime layout

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

## 12. Troubleshooting

**`arbiter-engine verification failed` during init** — the engine isn't
resolvable. The ladder falls back to the embedded engine automatically, so
this normally means `python3` itself is missing or broken — install python3
(≥ 3.9) and re-run `arbiter init`. To pin the embedded copy explicitly use
`arbiter init --embedded-engine`, or
install it (`pip install ./engine` from the Arbiter repo, offline-capable), and
ensure the right interpreter wins via `ARBITER_ENGINE_PYTHON`.

**Why can't the model read `.arbiter/playbook/` or match state?** By design (ADR-0015):
playbooks would reveal future steps and match files are the referee's. A PreToolUse guard
denies Bash/Read/Grep/Glob/Edit/Write access to those paths with a message naming the right
tool (ShowStepJob, ListTask, ReviewTask, CheckStepJob, ReadPlayBook, AddPlayBook,
NotePlaybook — ReadPlayBook is the curator's selection route). You, the human, are not
gated — edit playbooks freely in your editor; the guard fires on model tool calls only.

**"no active match" in the main session after the curator loaded a playbook** — match
state is shared through repo-local files, so this means the two seat processes disagree about
the repo root. Since ADR-0014 every entry init writes carries an explicit absolute `--root`;
the usual cause is a stale deployment from an older arbiter — re-run `arbiter init` (it
refreshes the player entry, all agent files, and the Stop hook). Moving the repo also requires
a re-init, same as for the binary path.

**gdb-mcp / perf-mcp "not connected", reconnect returns `-32000`** — the
server process exited on spawn. Since the companion entries became fully
absolute (command, `--root`, and embedded `PYTHONPATH`) and `arbiter init`
performs a real initialize handshake against both servers at deploy time, the
usual cause is a stale `.mcp.json` written by an older arbiter: re-run
`arbiter init` (idempotent — it refreshes the entries and re-verifies the
handshakes; a broken companion now fails *init* with `companion_verify_failed`
and the server's stderr, instead of failing silently in the session).

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
