# Arbiter User Guide

This is the detailed manual. If you just want working commands, use the Quick start in the
[README](../README.md) and come back here when something needs explaining.

**Maturity note.** Arbiter is mid-migration (see [migration.md](migration.md)). This guide
documents what works **today** and explicitly marks features that are designed but not yet
wired. The design of record is [design.md](design.md); when this guide and the design disagree
about the future, the design wins — about the present, this guide wins.

---

## 1. What you install

One artifact (ADR-0011):

```sh
make install        # → $HOME/.local/bin/arbiter  (override with PREFIX=…)
```

The single `arbiter` binary contains the Go referee (seats, Stop-hook gate, deploy) **and** the
embedded Python engine, which includes the bundled diagnostic MCP servers (ADR-0010):

- `arbiter_engine.gdbmcp` — structured GDB debugging, served under the MCP name **`gdb-mcp`**
- `arbiter_engine.perfmcp` — C performance triage, served under the MCP name **`perf-mcp`**

There is nothing else to download or pip-install. The one system prerequisite is **`python3`
(≥ 3.9)** — the engine is pure stdlib Python and needs an interpreter to run on; if it's
missing, `arbiter init` tells you so explicitly, and installing it is the only setup you will
ever be asked to do.

**How the engine resolves** (automatic, per repo, at `arbiter init`):

1. an **installed** `arbiter-engine` package for `python3` is preferred when present
   (`python3 -m pip install ./engine` from this checkout — optional, for people who want the
   engine on their interpreter);
2. otherwise the binary **materializes its embedded engine** into repo-local
   `.arbiter/engine/` — digest-tracked, re-materialized when you upgrade arbiter and re-run
   init, gitignored, and protected by Edit/Write deny rules so models can't patch the
   adjudication code;
3. only when `python3` itself is missing does init skip the diagnostics — loudly, with the fix
   in the output.

Upgrading arbiter = reinstall the binary, re-run `arbiter init` in each repo (it refreshes the
embedded engine and the wiring; it is always safe to re-run).

**Your build toolchain is never touched.** Arbiter never replaces or version-gates the
compiler your repo builds with — gcc/g++ of any vintage stays in charge of every build, and
the `arbiter cc` shim (when the build-driven indexing milestone lands) only journals each
invocation and execs your compiler bit-exact. The **facts index** is the one feature with its
own toolchain requirement: AST extraction parses with its *own* Clang + libclang (**LLVM
Clang ≥ 16 or Apple Clang ≥ 15**, located automatically), after cleaning gcc-only flags out
of the recorded compile commands — it never requires GCC, and your build never requires its
Clang. The two are isolated by design (inherited verbatim from cipher-2). No capable Clang on
the machine ⇒ no facts index (a typed, explicit failure on the gear-up verdict) — builds,
matches, shell/mcp predicates, and both bundled diagnostics keep working.

## 2. Setting up a repository — `arbiter init`

Run `arbiter init` once, from the root of the C/C++ repo you want to work in. It is
non-interactive, idempotent (run it twice, nothing changes), and finishes in seconds. It writes:

| Path | What | Notes |
|---|---|---|
| `.mcp.json` | `arbiter` server entry (`serve player`) | merge-preserving: your existing servers survive |
| `.mcp.json` | `gdb-mcp` + `perf-mcp` entries | only when the engine resolves (see below); launched via `python3 -m arbiter_engine.…`; existing same-name entries are never overwritten |
| `.claude/agents/arbiter-curator.md` | playbook-selection subagent | key-injected, 0600, gitignored |
| `.claude/agents/arbiter-debugger.md` | diagnose-and-fix executor subagent wired with gdb-mcp + perf-mcp | only when the engine resolves; key-injected, 0600, gitignored |
| `.claude/skills/arbiter-play/`, `.claude/skills/playbook-create/` | the user verbs | |
| `.claude/settings.json` | deny-read rules for match state + agent files; the Stop-hook gate | merge-preserving |
| `.arbiter/match/` | seat key, playbook dir, `FORMAT.md` | derived state is gitignored |
| `.arbiter/engine/` | the materialized embedded engine | embedded mode only; digest-tracked, Edit/Write-denied, gitignored |
| `.gitignore` | entries for the above | appended, deduplicated |

**Companion wiring condition:** init resolves the engine by the §1 ladder (installed package →
embedded materialization). The only way to end up without the diagnostics is a machine with no
`python3` at all — in which case init says so in its output and the fix is installing python3
and re-running `arbiter init`. In embedded mode the server entries carry
`env.PYTHONPATH=.arbiter/engine`; entry env always overrides the inherited environment.

**One manual step remains today:** the generic executor agent. `arbiter init` prints a complete
`.claude/agents/arbiter-executor.md` template (with your seat credential already inlined) —
paste it into that file. Automatic executor generation lands with the deploy rewrite (M7).
Note that the **debugger** executor IS auto-written, so diagnose-and-fix dispatch works out of
the box once the engine resolves.

## 3. Daily use

Open Claude Code from the repo. The verbs that work today:

- **`/arbiter-play <request>`** — start a refereed match. The curator subagent picks the
  playbook whose intent matches your request and loads it; the main conversation (the "player")
  walks the playbook step by step: it analyzes and dispatches tasks to executor subagents,
  executors do the work and submit machine-checkable results, the referee adjudicates by
  counting verdicts — the model never declares its own success. While a match is live, the
  Stop hook blocks the model from quietly stopping; user interrupts are unaffected.
- **`/playbook-create`** — interview → draft → register a new playbook for knowledge you want
  to keep. Playbooks are committed files; they compound.
- During play the player appends one-line **gotchas** to playbook steps (`NotePlaybook`) as it
  trips over things — the next match over that playbook sees them.

Designed but not yet present: `/arbiter-intro` (the adjudicated bootstrap match that derives
and proves build recipes — M7), `search`/`detail` fact tools and `run` recipe tools on the
seats (M4/M5).

## 4. Playbooks

Playbooks ("openings") live in `.arbiter/match/playbook/*.md`. The authoritative grammar is
written into your repo at `.arbiter/match/FORMAT.md` by init; in short: YAML frontmatter
(`name`, `description`, optional `max_steps`), then `[STEP] <id>` blocks each carrying
`[StepJob]`, `[CheckList]`, `[Branch]` (`success:`/`failure:` targets or `END`), optional
`[Gotcha]` items, and at most one match-level `[SetGoal]` before the first step.

`[SetGoal]` declares the checkmate predicate, evaluated after every successful round:

```markdown
[SetGoal]
shell: make test
```

or against an MCP server, with typed field comparison:

```markdown
[SetGoal]
mcp: perf-mcp perf.measure_command
arguments: {"command": ["./bench"], "repeat": 5}
expect: [{"path": "summary.all_successful", "op": "eq", "value": true}]
```

Without `expect`, an mcp goal passes on any non-error response — prefer `expect` whenever the
server reports structured results (both bundled servers do).

Four starter openings ship with arbiter — `arbiter init` writes them into
`.arbiter/match/playbook/` (write-if-missing: your edits are never overwritten):

| Opening | Use when | The referee mechanism inside |
|---|---|---|
| `fix-reported-bug` | a known crash/misbehavior must die | deterministic-repro contract: 5x all-fail loop proves the repro, then ONE predicate = repro-file-unmodified (`git diff --quiet`) + 5x green + suite green |
| `hunt-latent-bugs` | find defects nobody pinned down | symptom-test polarity: the test passes iff the bug exists, so `build && run` exit 0 is a machine proof — prose claims don't adjudicate |
| `build-feature` | new functionality, scenario-first | `build && ! run` proves tests are red for the right reason; test untouchability rides every later predicate |
| `fix-slow-path` | something is measurably slow | expect-clause measurements; two baselines define the noise band; a gain must beat the band or the change reverts |

Naming convention (binding, enforced by CI on the shipped set and by `/playbook-create` on
yours): the name is the *user intent* as an imperative phrase — verb-first, kebab-case, at
most 3 segments, file stem equals the name. Descriptions lead with "Use when …" and
cross-point "Do not use … (use <other>)" wherever intents are adjacent, so the curator
deduplicates at selection time. Steps state the exact result predicate the executor must
submit; laws live inside predicates, not prose. The full rules are in your repo's
`.arbiter/match/FORMAT.md` after init.

## 5. Verifiable results — the predicate language

Executors finish tasks with `SubmitTask{task_id, summary, report, result}`. The `result` is a
**ResultSpec** the referee executes itself — never trusted prose. Kinds working today:

**`shell`** — `{"kind":"shell", "command":"make test"}`. Passes iff exit code 0. Output is
captured bounded (`output_lines`, default 256; `timeout_s`, default 600, max 3600).

**`mcp`** — call a tool on a server from `.mcp.json` and judge the response:

```json
{"kind": "mcp", "server": "perf-mcp", "tool": "perf.measure_command",
 "arguments": {"command": ["./bench"], "repeat": 5},
 "expect": [
   {"path": "summary.all_successful", "op": "eq", "value": true},
   {"path": "summary.median_wall_seconds", "op": "le", "value": 2.5}
 ]}
```

Without `expect`, the call passes iff the tool did not return an error (`isError=false`) — the
weakest signal; fine for probes, wrong for anything with structured results. With `expect`,
the referee compares the tool's `structuredContent` field by field:

| Rule | Detail |
|---|---|
| ops | `eq`, `ne`, `ge`, `le`, `exists` — closed set, no wildcards or string matching |
| values | scalars only (string/number/bool); `ge`/`le` require numbers; `exists` takes none |
| paths | dot-separated: object keys and array indices (`checks.0.ok`) |
| count | at most 8 clauses; all must hold (AND) |
| fail-closed | missing path or type mismatch fails the clause — including `ne` |
| isError gate | an errored call fails the verdict even if every clause matches |
| review | per-clause `{path, op, value, actual, ok}` report is stored on the task — `ReviewTask` shows it |

A server entry whose command resolves to the arbiter binary itself is rejected
(`reserved_server`) — the referee is not a valid evidence source for its own verdicts.

**`run` / `fact`** — typed build/test and AST-fact predicates
(`{kind:"run", tests:[…], expect:{overall:"passed"}}`). Schemas are final and validated at
submit time, but their evaluators ride the engine integration that lands in M4/M5 — submitting
one today returns a typed `engine_unavailable` error rather than a fake verdict.

## 6. Bundled diagnostics in depth

### gdb-mcp (`python3 -m arbiter_engine.gdbmcp`)

Structured GDB/MI debugging for agents — typed JSON in `structuredContent`, never scraped
terminal text. Tools:

| Tool | Purpose |
|---|---|
| `gdb_start` | start a session: `exec` (default), `core`, opt-in `attach`/`remote`; optional `run_until: main|entry` |
| `gdb_exec` | run / continue / next / step / finish / until / interrupt / wait |
| `gdb_breakpoint` | set/list/delete/enable/disable/clear breakpoints **and watchpoints** (`kind: watch|rwatch|awatch` — the memory-corruption workhorse) |
| `gdb_select` | select thread / frame |
| `gdb_stack` | bounded backtrace + optional source context |
| `gdb_snapshot` | one call: stop reason, threads, stack, locals, args, registers |
| `gdb_eval` | expression, type, locals, args, registers, threads |
| `gdb_memory` | bounded byte reads (≤4096) |
| `gdb_command` | guarded console escape hatch — `shell`, `python`, `source`, `dump`, … are denied unless the server runs with `--allow-dangerous-commands` |
| `gdb_sessions` / `gdb_stop` | inventory / cleanup |
| `gdb_diagnostics` | the doctor checks as a tool |

Security defaults: paths confined to the repo root, attach/remote disabled, dangerous console
commands denied, everything bounded. To opt in, edit the `gdb-mcp` entry's `args` in
`.mcp.json` (e.g. add `--allow-attach`). Session state and a redacted audit log live in
`.gdb-mcp/` (gitignored by the wiring).

### perf-mcp (`python3 -m arbiter_engine.perfmcp`)

C performance triage with measurement evidence:

| Tool | Purpose |
|---|---|
| `perf.scan_c` | ranked findings with stable rule ids (`C.PERF.ALLOC_IN_LOOP`, `C.PERF.STRLEN_IN_LOOP`, `C.PERF.REALLOC_GROW_ONE`, nested-loop, bulk-memory, IO, expensive-math), file:line evidence, severity/confidence, budgets (250 files / 5 MB default) |
| `perf.explain_finding` | per-rule false-positive checks, safe fix strategy, measurement plan |
| `perf.measure_command` | run an **argv array** (shell strings rejected) N times: wall/user/system seconds, max RSS, median summary |
| `perf.toolchain_probe` | which compilers/profilers exist here (`perf`, `dtrace`, `xctrace`, `valgrind`, `hyperfine`, …) |

All results are schema-versioned (`perf-mcp.scan.v1`, `.measure.v1`, …) — the field names your
`expect` clauses target are stable within a version.

### The arbiter-debugger agent

`.claude/agents/arbiter-debugger.md` is an executor-seat agent specialized for
diagnose-and-fix tasks: it pins crashes with GDB evidence (snapshot at the stop, watchpoints
for corrupting writes), triages perf with scan → explain → measure-before/after, falls back to
plain Bash when a diagnostic tool can't start, and submits typed predicates. Playbooks that
deal in crashes or performance say "prefer the arbiter-debugger agent" — dispatch those tasks
to it.

A scan is **triage, not proof**: a finding means "inspect and measure this path", which is why
the perf playbook insists on a measured baseline and a measured gain before checkmate.

## 7. Doctor & troubleshooting

**Check debugger readiness:**

```sh
python3 -m arbiter_engine.gdbmcp doctor --root .
```

The probe is stricter than `gdb --version` — it compiles a one-liner and checks GDB can
actually run it. Real output from a macOS machine with Homebrew GDB:

```
ok: python - /Library/Developer/CommandLineTools/usr/bin/python3
ok: package - arbiter_engine.gdbmcp 0.1.0
ok: gdb - /opt/homebrew/bin/gdb (GNU gdb (GDB) 17.2)
missing: gdb_run - Don't know how to run.  Try "help target".
ok: root - /Users/you/your-repo
```

That `gdb_run` failure is the classic Darwin limitation: GDB parses symbols but cannot launch
local inferiors without codesigning (and Apple-silicon support is limited). Your options:
codesign gdb, debug against a remote target (`--allow-remote`), or do live debugging on a
Linux box — static perf triage and all of perf-mcp work everywhere. When GDB *can* run, build
targets with `-g -gdwarf-4 -O0` for first-pass debugging; the server returns typed guidance
(`debug_info_format_unsupported`, `darwin_gdb_codesign_required`) when it recognizes these
failures.

**Companions weren't wired by init** — the `.mcp.json` has no `gdb-mcp`/`perf-mcp` entries and
init's output mentions python3. The only cause: no `python3` on PATH. Install python3 (≥ 3.9)
and re-run `arbiter init` — the engine itself ships inside the binary, so there is nothing else
to install.

**A predicate failed and you want to know why** — `ReviewTask{task_id}` shows the verdict, the
captured output, and for `expect` predicates the per-clause report with actual values.

**Where state lives:**

| Path | What | Committed? |
|---|---|---|
| `.arbiter/match/playbook/` | playbooks | yes |
| `.arbiter/match/run/`, `.arbiter/match/log/`, `status.json` | match state, seat key, journal | no (gitignored) |
| `.gdb-mcp/audit.jsonl` | gdb-mcp audit (summaries only, secrets redacted) | no |
| `.claude/agents/arbiter-*.md` | seat agents with inlined credential | no (gitignored; re-run init on a new machine) |
