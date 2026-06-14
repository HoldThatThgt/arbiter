# Arbiter — One Referee-Adjudicated Dev Loop for C DBMS Codebases

**Executive summary.** Arbiter unifies cipher-2, chess, and crun-mcp into a single product whose unit of value is the loop an LLM runs when developing C in a large **gtest-guarded C DBMS** (the real target; the postgres/sqlite sibling checkouts are dummy benchmarks only): plan via playbook → gear up (build with the request's profile, which *is* the index) → orient on AST facts → dispatch executors → edit → build/test → verify with machine-checkable evidence → learn. A deterministic Go referee (chess, kept) owns every transition; its 1-bit predicate language is promoted into typed evidence claims with two native evaluators — the fact engine (cipher-2, kept) and the run engine (crun, gtest-first behind a thin harness seam) — so "this run's `overall==passed`" and "`reachable:A->B` is complete and empty" become adjudicable verdicts, while natural language and string matching still never adjudicate anything. The facts index has **no standalone lifecycle**: a compiler-launcher shim makes every `src_compile` emit the compile database and extract changed TUs concurrently, so when the build is green the index is published — there is no `cipher2 init`, no indexing ceremony, no waiting that the build itself doesn't impose. Everything the loop emits lands in one repo-local `.arbiter/` store, stamped with shared `{match_id, task_id, run_id}` correlation IDs. The user contract is four verbs: `arbiter init` once (shell), `/arbiter-intro` once (in-session bootstrap), then `/arbiter-play <request>` per request and `/playbook-create` where skill-create used to be — beyond that, Claude Code behaves as if Arbiter didn't exist. This beats three isolated tools because today no data flows between them: chess predicates can't see crun's per-test results (the gold-digger false checkmate — `isError=false` on failing tests), cipher facts never reach executor context or playbook verification, cipher never knows how the tree is built, crun rebuilds blindly after every restart, and deployment is a documented 5-step manual ritual. Arbiter fixes all five with mechanisms, not conventions.

---

## 1. Current State

### cipher-2 — the fact oracle (keep the core, fix the wiring)
A Python-stdlib-only FACT runtime: type-driven libclang AST extraction of `TheFact`/`FactRelative`/source inventory into content-addressed `.cipher/` snapshots, served via a frozen two-tool stdio MCP surface (`search`/`detail`) with a relation mini-language (`readers:`, `callers:`, `dispatches_via:`, `reachable:A->B`), bounded BFS, and a byte-exact 8/32/128KB response-degradation ladder. **Worth keeping:** the entire fail-closed extraction pipeline (capability probe, map-reduce, linkage-aware call resolution), the snapshot lifecycle, the budget engine, the weak-model query UX (anchor tiers, copyable examples, honest `complete`/`budget_exhausted` flags), the redaction-disciplined JSONL logging, and the merge-preserving atomic `.mcp.json` writer pattern. **Not worth keeping:** the venv-pinned `python -c` launcher (breaks when the venv moves), the never-started incremental poll thread and dead `overlay_ttl_seconds` knob, build-blindness (it consumes `compile_commands.json` but has no concept of how it's produced), the standalone init/rebuild ceremony as the only way to obtain an index (a separate hours-long pre-step the daily loop cannot afford — in the unified product indexing rides the build the agent runs anyway), and overlay-mode O(repo) relation scans as a long-term posture.

### chess — the referee (keep nearly verbatim, extend the predicate language)
A ~3.1k-LOC Go playbook orchestrator: player analyzes/dispatches only, executors submit machine-checkable predicates, a deterministic referee adjudicates by pure counting under flock with two-phase lock protocols; constructive RBAC via per-seat MCP servers + startup credentials; `[SetGoal]` checkmate, Stop-hook gating, budgets; append-only gotchas; the best init of the three (idempotent structured merges). **Worth keeping:** essentially all of it — the state machine, seat isolation, two-phase protocols, journal, deploy skeleton. **Not worth keeping:** 1-bit verdicts (exit code/isError) that can't consume structured run/fact results; the path-equality `reserved_server` guard (self-blocking in a unified binary); spawn-per-predicate cold starts; goal re-execution after every successful round; the trailing-words Stop-hook claim heuristic; the manual executor-agent creation step; minor drift (curator ListTask whitelist, dead `AbortReplaced` constant).

### crun-mcp — the execution prover (keep the doctrine, generalize the harness, rebuild the surface)
A Python MCP server turning build/run knowledge into committed YAML recipes with a proven/unproven lifecycle; per-test results only from injected `--gtest_output` structured files; clean committed/derived/trace storage tiers; privacy-redacting audit log. **Worth keeping:** the proven-verdict epistemics (a run that compiles+launches+parses is the sole proof; doc-only edits preserve it), the structured-output-only invariant, recipe portability rules, the pre/cmd/post stage semantics, the docstring-as-playbook style, the fake-harness test strategy. **Not worth keeping:** gtest *hardwiring* — gtest is the right center of gravity (the real target DBMS is gtest-guarded; pg_regress/TAP matter only for the dummy benchmarks), but the coupling should be a first-class gtest adapter behind a thin harness seam rather than gtest assumptions threaded through runner/scanner/state — plus the process-memory compile cache with wrong staleness polarity (stale binaries silently reused after edits), no cross-process safety, the FastMCP/pydantic/PyYAML/tree-sitter dependency stack (conflicts with the zero-dep engine merge), its separate init/config files, and the duplicate tree-sitter AST stack.

---

## 2. Unified Architecture

### Components

- **`arbiter` (Go, single static binary, vendored deps):** chess's six packages plus `internal/engineclient` (a minimal hand-rolled line-delimited JSON-RPC 2.0 stdio client — cipher's server is itself a hand-rolled line loop, so the matching client is ~300 LOC, golden-transcript-tested) and a rewritten `internal/deploy`. The referee, seats, Stop hook, and init live here.
- **`arbiter-engine` (Python ≥3.9, stdlib-only):** cipher-2 absorbed as `facts/`, crun rebuilt on cipher's MCP loop as `runs/` with harness adapters, plus `shared/` (work-tree census, disk build cache, compile-db management, unified config/logging) and the bundled companion servers `gdbmcp/`/`perfmcp/` (ADR-0010). **Delivery is one artifact (ADR-0011):** the engine ships embedded in the Go binary; `arbiter init` materializes it into repo-local `.arbiter/engine/` (digest-keyed, Edit/Write-denied, gitignored) unless an installed `arbiter-engine` package already resolves for `python3` — the installed package stays preferred, pinned by machine-local `.arbiter/run/engines.json` (M7), and spawn-time digest verification of the adjudication evaluator lands with engineclient spawning per ADR-0007.

```
TARGET REPO (your gtest-guarded C DBMS; pg/sqlite checkouts = dummy benchmarks)
┌──────────────────────────────────────────────────────────────────────────────┐
│ Claude Code session                                                          │
│   player (main conv)        curator subagent        executor subagents (xN)  │
│        │ stdio MCP               │ stdio MCP               │ stdio MCP       │
│        v                        v                         v                  │
│ ┌─────────────────┐   ┌─────────────────┐   ┌──────────────────────┐         │
│ │ arbiter serve   │   │ arbiter serve   │   │ arbiter serve        │  Go     │
│ │   player        │   │   curator       │   │   executor           │  seat   │
│ │ referee tools   │   │ ReadPlayBook    │   │ SubmitTask/List/Rev  │  procs  │
│ │ + search/detail │   │ LoadPlayBook    │   │ + search/detail      │         │
│ │ (goal predicates│   │ List/Review     │   │ + run/recipe tools   │         │
│ │  settle here)   │   │                 │   │ (task predicates run │         │
│ └──────┬──────────┘   └─────────────────┘   │  here)               │         │
│        │                                    └─────────┬────────────┘         │
│        │  each seat owns its engine children:         │                      │
│        │  QUERY engine (eager, facts) +               │                      │
│        │  EXEC engine (on first run use)              │                      │
│        v                                              v                      │
│ ┌──────────────────────────────────────────────────────────────────┐         │
│ │ arbiter-engine (Python stdlib, stdio JSON-RPC, per-seat children)│         │
│ │  facts: search, detail        (snapshots + overlay, budgets)     │         │
│ │  runs : run, recipe_search, register, import_recipes, scan       │         │
│ │  rpc  : arbiter/refresh /census /resolveBriefing                 │         │
│ │         /startRun /runStatus  (referee-only methods, not tools)  │         │
│ │  shared: work-tree census · disk build cache · compile-db        │         │
│ │  ROLE: player's query engine = sole facts WRITER; others READ   │         │
│ └──────────────────────────────────────────────────────────────────┘         │
│                                                                              │
│ state: .arbiter/{match/, facts/, runs/, locks/, log/, status.json, run/}    │
│ committed: .arbiter/playbook/*.md  .arbiter/recipes.yaml  .arbiter/config.yml│
│ wiring: .mcp.json (ONE entry) · .claude/{settings,agents,skills}             │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Process model
No daemons, stdio only, repo-local state — the inherited posture. Claude Code spawns `arbiter serve player` (session-resident) from `.mcp.json`; curator/executor seats spawn per-subagent via inline `mcpServers` frontmatter. Each seat **eagerly** spawns one QUERY engine child at birth (cheap: the engine defers overlay reconcile until first fact access — an explicit, owner-signed engine change) and lazily spawns one EXEC engine child on first run-tool or run-predicate use; the two-child split eliminates head-of-line blocking (a one-hour goal run never blocks `search`). `tools/list` is always forwarded from the live QUERY engine, so Go-pinned schema drift is structurally impossible; golden stdio transcripts still gate field shapes in CI. Seats `Setpgid` their engine children, kill the group on exit, and engines self-exit on stdin EOF (tested on darwin, which has no parent-death signal). Long runs started via `arbiter/startRun` execute in a double-forked, process-grouped worker bounded by its own timeout, writing results to SQLite — a bounded job, not a daemon, and settle-able even across a seat restart.

Concurrency is governed by an explicit lock inventory (grafted from design B) under `.arbiter/locks/`: `match.lock` (referee state, flock + atomic dual-write, unchanged), `snapshot.lock` (facts publish), `overlay.lock` (overlay publication by the single writer engine), `state.lock` (runs SQLite book/recipe writes, with `BEGIN IMMEDIATE` for proven-lifecycle read-modify-writes), and `build/<sha8(workdir)>.lock` (serializes src_compile/test_compile/test_run per workdir across **all** engine children, including the player's goal runs — concurrent `make` in one tree is never allowed). The claim is therefore "**DB-safe and build-serialized**" — full parallel-executor throughput claims wait on contention tests at 8+ fan-out. Facts single-writer rule: only the player's QUERY engine reconciles and publishes overlays; all other engines read base snapshot + the latest *published* overlay (atomic pointer), and every fact-predicate evidence records `{snapshot_id, overlay_id, view_state}` so adjudication views are deterministic and auditable. `arbiter init` probes the filesystem and **fails closed with a typed error on network mounts** (flock/WAL semantics require a local FS).

### Data flow — the loop
1. **PLAN:** user invokes `/arbiter-play <request>`; curator `LoadPlayBook` freezes the matching opening (or the generic `freeplay` opening when none matches) **and pins `recipes.yaml`** (book sha256 + per-target content hashes) into match state. Every opening's first step is a conventional **gear-up** step, templated by `/playbook-create`.
2. **GEAR-UP (build ⇒ index):** the player derives the build profile from the user's request — `asan` for memory corruption, `coverage` for test-gap work, `debug` otherwise, plus any feature flags the request names — and dispatches a build task whose predicate is `{kind:"run", stage:"src_compile", profile:[...], expect:{overall:"passed", facts:{published:true}}}`. The interposed build (see *Build-driven indexing* below) journals every compiler invocation and extracts changed TUs concurrently; when the build goes green the snapshot publishes. **Compile done ⇒ index done** — there is no other indexing ceremony, and a fresh clone's first gear-up *is* the cold index.
3. **ORIENT:** player calls `search`/`detail` → proxied verbatim to its QUERY engine, answering from the just-published snapshot; correlation `{match_id, round}` travels in JSON-RPC `_meta` (never in tool arguments — cipher's closed schemas survive untouched).
4. **DISPATCH:** player `CreateTask{request, fact_refs:[object_id]≤8, verify?}` → referee resolves refs via `arbiter/resolveBriefing` (per-id `detail(budget=small)`, ≤8KB total) → briefing cards stored on the Task; unresolvable refs fail closed with typed `briefing_unresolved{bad_refs[]}` — never silently dropped.
5. **EDIT:** executor uses host tools.
6. **BUILD/TEST:** executor calls `run`. Cache hits require the stage key **and** an unchanged census over the recipe's declared `sources:` globs (direct work-tree scan with new/deleted-file detection — not the snapshot inventory); recipes without `sources:` never get cross-process cache hits. Per-test results come only from the harness adapter's injected structured result file. Any `src_compile` stage re-runs through the same interposed path, so the index tracks every rebuild for free.
7. **VERIFY:** executor `SubmitTask{result:{kind:"run", recipe:"btree_tests", tests:["VacuumLock.*"], expect:{overall:"passed"}}}`. The referee verifies the pinned recipe hash, executes via the seat's EXEC engine, compares `expect` field-by-field, and stores verdict + typed `evidence` + per-clause `expect_report`.
8. **ADJUDICATE:** `CheckStepJob` counts pass/fail (unchanged). Run-kind goals execute asynchronously: first call starts the run and returns `{complete:false, reason:"goal_running", run_id}`; subsequent calls poll `arbiter/runStatus` and settle two-phase. Goal memoization (census digest folding in toolchain hash, goal-spec hash, recipe-book hash) ships **default-off**.
9. **LEARN:** `NotePlaybook` gotchas (unchanged, append-only); proven lifecycle updates; `arbiter report` joins journal + run rows for post-match curation.

### Build-driven indexing — the index is a by-product of the build

The facts store has no standalone lifecycle: no `cipher2 init`, no user-facing index command, no pre-indexing wait. The first profiled build of the tree *is* the cold index, and every subsequent build keeps it fresh.

- **Compiler interposition.** When the EXEC engine runs a `src_compile` stage it injects a launcher shim (`CC="arbiter cc -- <real-cc>"` / `CMAKE_C_COMPILER_LAUNCHER`, installed into the recipe by `/arbiter-intro`). The shim is a **subcommand of the Go binary, not a Python entrypoint** — it is exec'd once per TU, thousands of times per full build, so its startup cost multiplies: a Go process starts in ~1–3ms where a Python interpreter costs tens of ms, the difference between an invisible tax and a minute-plus of added build time at DBMS scale. For each TU the shim (a) appends the exact argv+cwd to a build-local compile-db journal — `compile_commands.json` falls out of **any** build system for free, no bear/cmake-export dependency, and the journaled set is *exactly* the actually-built TU set, strictly more authoritative than a static file (honoring cipher's "compile database is the authoritative TU set" invariant in its strongest form) — and (b) enqueues the TU for extraction, then execs the real compiler unchanged. The shim is **fail-open for the build** (on any internal defect it execs the compiler and journals the miss — a shim bug can never break compilation) while snapshot publication stays **fail-closed** (cipher's `clang_ast_failed`/`clang_ast_partial` policy reports per-file extraction failures in the publish summary, surfaced on the run verdict). **Two toolchains, isolated** (cipher-2's contract, kept): `<real-cc>` is the repo's own build compiler — gcc/g++ of any vintage is the normal DBMS case — and is never substituted or version-gated; extraction parses the journaled TUs with its *own* Clang/libclang (LLVM Clang ≥ 16 / Apple Clang ≥ 15, capability-probed, runtime-located) after allowlist-cleaning the build argv, and the AST path never requires GCC. A host without a capable Clang loses facts publication (typed failure on the gear-up verdict), never the build.
- **Overlapped extraction.** A bounded worker pool (default `cores/4` while compilers are running, full width after the build's last TU) consumes the queue using cipher's existing per-file process workers. The `run` tool's `src_compile` stage returns only after build-green + queue drained + snapshot published, and its RunResult carries `facts:{published, snapshot_id, files, warnings, extract_ms, hidden_ms, tail_ms}` — one typed verdict adjudicates "compiled AND indexed", and the overlap economics are measured, never asserted.
- **Two caches, two keys.** The *build* cache keys on the full flag set + profile (an ASan binary is not a plain binary). The *extraction* cache keys on `(TU content, include-closure content, allowlist-cleaned semantic flags, toolchain id)` — exactly the flags cipher's parser sees. The allowlist is pinned to strip codegen-only flags (`-O*`, `-g*`, `-fsanitize=*`, `--coverage`, `-fprofile-*`), so **switching profile re-extracts nothing**: an asan or coverage rebuild reuses the entire facts index by construction, while feature-selection changes (`-DWITH_X`, a regenerated `config.h`) invalidate precisely the TUs whose include closure they touch. (Known blind spot, documented in Risks: code gated on *compiler-injected* instrumentation macros like `__SANITIZE_ADDRESS__` is indexed in its plain configuration unless those flags are opted into `facts.key_flags`. `/arbiter-intro` scans for exactly these tokens at bootstrap and surfaces a `key_flags` recommendation, so the caveat is detected, not remembered. Explicit `-D` defines and generated config headers are *not* in this class — they live in the key already. `--coverage` defines no macros at all, so the coverage profile is safe unconditionally.)
- **Bootstrap & freshness.** A fresh clone has no snapshot: `search`/`detail` return a typed `no_snapshot{hint:"run the gear-up step"}` until the first build. Between builds, executor edits stay visible through the existing overlay reconcile path (`arbiter/refresh` before fact predicates) — two freshness paths, one store; a build-published snapshot resets the overlay.
- **Performance contract.** Added wall time on a full build ≈ max(0, extraction_time − idle CPU during the build) + publish tail. libclang parse (front-end only, no codegen) generally costs less than the same TU's `-O2` compile, so on a parallel build extraction mostly hides inside the build's own wall time; incremental builds extract only changed TUs. The contract is observable on every src_compile verdict via `facts:{extract_ms, hidden_ms, tail_ms}`.

---

## 3. Component Mapping

| Existing module | Disposition | Destination / note |
|---|---|---|
| cipher-2 `initializer/extractor/code/*` (streaming, mapper, ast_backend, toolchain, direct_calls) | **Kept** | `arbiter-engine/facts/` — fail-closed core untouched; ENDGAME items continue here |
| cipher-2 `storage/*` (snapshot_writer, read_index, search, views) | **Kept** | `facts/`; inventory hashing code **refactored** into `shared/census` (generalized to arbitrary file sets) |
| cipher-2 `incremental/` | **Kept/refactored** | reconcile made lazy + writer-role-gated; poll thread & `overlay_ttl_seconds` **dropped**; overlay publish takes `overlay.lock` |
| cipher-2 `mcp/` (server loop, descriptors, budget ladder) | **Refactored** | engine-wide JSON-RPC core hosting all namespaces + `_meta` extraction; `search`/`detail` schemas byte-identical |
| cipher-2 `cli.py` init/rebuild/status | **Rewritten** | engine batch mode behind CI-only `arbiter index` + `status`; interactive indexing moves to the gear-up build path; its `.mcp.json` writer **dropped** (Go deploy owns wiring) |
| cipher-2 `config/`, `tools/log` | **Refactored / kept** | `.arbiter/config.yml` `facts:` section; redaction rules become the facts/runs channel standard |
| cipher-2 `knowledge/` | **Dropped from runtime** | authoring source for the openings playbook library |
| chess `internal/match` | **Kept, extended** | evidence + expect_report on Task, `goal_memo`, `goal_pending`, briefing fields; dead constants deleted |
| chess `internal/verify` | **Refactored** | adds `run`/`fact` kinds + `expect[]` on mcp-kind; `reserved_server` becomes **deny-self outright** for mcp-kind |
| chess `internal/playbook` | **Kept, extended** | `[Verify]` blocks, run/fact `[SetGoal]`, `capabilities:` frontmatter; tokenizer regex-free as ever |
| chess `internal/seat` | **Kept, extended** | proxied engine tools; capability-gated registration with fail-closed edge semantics; curator ListTask drift fixed |
| chess `internal/journal` | **Kept** | full-fidelity forensics retained (see Risks); correlation IDs added |
| chess `internal/deploy` | **Rewritten** | the unified init; structured-merge skeleton retained; **exact-command** Stop-hook claim (graft) |
| chess `cmd/chess` | **Extended** | `cmd/arbiter`: init/adopt/index/status/report/serve/hook |
| — (new) `internal/engineclient` | **New** | minimal JSON-RPC client; golden-transcript contract tests |
| — (new) Go `internal/interpose` (`arbiter cc` subcommand) | **New** | per-TU compiler launcher in the ms-startup Go binary (a Python shim would tax every compile with interpreter startup): journals argv+cwd → compile-db, appends to the extraction queue, execs the real compiler; fail-open for the build, adversarially tested (response files, ccache stacking, `make -jN`, interrupted builds) |
| crun `runner.py` | **Refactored** | `runs/`: harness-adapter seam; census-validated disk cache; per-workdir build lock; per-target reload fixed; `run_test` PK gains `occurrence` |
| crun `models.py` (pydantic) | **Rewritten** | stdlib dataclasses, schemas preserved; strictness pinned by tests |
| crun `recipes.py` | **Refactored** | strict stdlib YAML-subset parser; gains `harness:`, `sources:`, `profiles:`, `compile_db:`; portability rules kept |
| crun `state.py` | **Kept, extended** | WAL + busy_timeout + `BEGIN IMMEDIATE`; correlation columns; `compile_cache` table |
| crun `scanner.py` (tree-sitter) | **Demoted** | facts-derived gtest discovery (read_index query for TestBody facts) is primary; tree-sitter = optional `[scan]` extra for cold repos, fail-closed when absent |
| crun `server.py` (FastMCP) | **Dropped** | tools re-registered on the engine's stdlib loop; docstring-as-playbook style preserved |
| crun `audit.py` | **Merged** | runs-channel redactor (length-only summaries) in unified logging |
| crun `project_init.py`/`cli.py`/`crun-mcp.toml` | **Dropped** | Go init owns deployment; config folds into `config.yml` |
| crun skill template | **Rewritten** | committed recipe-derivation playbook + slim unified skill |
| gold-digger playbook | **Kept, rewritten** | ships as an opening using typed run/fact predicates |

---

## 4. Unified Repo-Local State & Config

```
<repo>/
  .mcp.json                        # ONE entry: arbiter → serve player (atomic, merge-preserving)
  .claude/settings.json            # deny: Read(.arbiter/playbook/**), Read(.arbiter/match/**),
                                   #       Read(.claude/agents/arbiter-*.md);
                                   # Stop hook claimed by EXACT command match
  .claude/agents/arbiter-{curator,executor,implementer,test-author,debugger}.md  # init-written, key-injected, 0600, gitignored
  .claude/skills/arbiter-play/  arbiter-intro/  playbook-create/    # the three user-facing verbs
  .arbiter/
    config.yml                     # COMMITTED. strict YAML-subset. sections:
                                   #   facts:{extractor, incremental, index_on_build:{pool, key_flags}}
                                   #   runs:{harness defaults}
                                   #   match:{goal_memo: false}        engine:{}
    playbook/*.md                  # COMMITTED — steps, [CheckList], [Branch], [Gotcha], [Verify], [SetGoal], [Submit], [Checkpoint]
    recipes.yaml                   # COMMITTED — RecipeBook v2:
                                   #   vars,
                                   #   profiles:{debug,asan,coverage,…: flag/env overlays on compile stages},
                                   #   compile_db:{path, target?},  # journaled by the cc shim during
                                   #                                # src_compile; target = explicit
                                   #                                # generator fallback if shim is off
                                   #   targets[{id, binary, tests, workdir, env,
                                   #     harness:{kind: gtest, ...},  # gtest first-class; exitcode/
                                   #                                  # pgregress/tap optional, later
                                   #     sources:[globs],            # cache/memo census scope
                                   #     requires, notes, src_compile/test_compile/test_run}]
    match/                         # DERIVED: state.json (0600), seat.key — chess layout, relocated
    facts/                         # DERIVED: snapshots/{current,<id>/...}, run/{mapreduce,incremental},
                                   #   extract-cache/ (semantic-key per-TU cache)
                                   #   — cipher v5/v6 layout verbatim, relocated
    runs/                          # DERIVED: state.sqlite (WAL: scanned_test, run(+match_id,task_id,round),
                                   #   run_test(+occurrence), run_payload, target_state,
                                   #   compile_cache{key, sources_digest, binary, built_at}),
                                   #   runs/<run_id>/ artifacts
    locks/                         # match.lock, snapshot.lock, overlay.lock, state.lock, build/<h>.lock
    run/engines.json               # MACHINE-LOCAL, gitignored: {python, engine_version, verified_at}
    log/                           # TRACE: journal.jsonl (referee, full-fidelity, 0600, fsync),
                                   #   facts.jsonl, runs.jsonl (redacted channels);
                                   #   all events carry {match_id?, round?, task_id?, run_id?}
    status.json                    # 0644, REFEREE-ONLY match projection (never future steps);
                                   #   facts/runs status composed on read by `arbiter status`
```

The three stores **coexist as subtrees** with their own lifecycles (facts: content-addressed, rebuild-only; runs: derived deletable SQLite; match: ephemeral JSON — all three already disclaim derived-state compatibility), joined by the shared census service, correlation IDs, and the pinning of committed knowledge (playbook + recipe hashes) into match state. Key schema deltas: `Task.briefing[]` (≤8KB, pruned from archived rounds — journal retains them), `Result.evidence` + `expect_report[]`, `Match.recipes_pin{book_sha256, targets{id:sha256}}`, `Match.goal_memo`/`goal_pending`. Adjudication consumes only verdict enums and counters; evidence enriches review and reporting, never the verdict.

---

## 5. Complete Tool Surface

### CLI (`arbiter`, Go)
| Command | Who | Purpose |
|---|---|---|
| `arbiter init [--no-executor] [--remove] [--embedded-engine]` | user | the one shell-side init: engine resolution → engines.json, seat key, agents (curator, executor, implementer, test-author, debugger; key-injected), skills, structured merges of .mcp.json/settings/.gitignore; idempotent, non-interactive, **seconds — never builds or indexes** |
| `arbiter adopt` | user | migrate `.chess/.cipher/.crun-mcp` committed knowledge into `.arbiter/`; deletes/regenerates derived state; emits a **manual checklist** of files containing legacy whole-token tool names (no automated prose rewriting — constitution forbids it) |
| `arbiter index [--rebuild] [--compile-database P]` | CI/recovery only | **planned — not yet a CLI subcommand**; headless batch extraction for CI bots and disaster recovery; **not part of the interactive path** — gear-up builds own indexing |
| `arbiter status [--json]` | user | compose-on-read aggregation: match (from status.json) + facts + runs (queried from engine) |
| `arbiter report [match_id]` | user | post-match digest joining journal + run rows |
| `arbiter serve <player\|curator\|executor>` / `arbiter hook stop` | host | seats and the fail-open Stop gate |
| `arbiter cc -- <real-cc> [args…]` | build system (via recipes) | the interposition shim: journal argv+cwd, enqueue TU, exec the real compiler; never invoked by users or models directly |

### The user contract — two inits, then invisible

| When | User does | What happens underneath |
|---|---|---|
| once per repo (shell) | `arbiter init` | wiring only: engines.json, seat key, agents, skills, `.mcp.json`/settings/`.gitignore` merges. Seconds. |
| once per repo (in CC) | `/arbiter-intro` | an **adjudicated bootstrap match**: probes the build system, derives + proves recipes (compile → launch → structured gtest output, refereed — design B's adjudicated recipe authoring, repurposed as the intro), installs the `arbiter cc` shim into `src_compile` stages, runs the **instrumentation-macro scan** (whole-token grep for `__SANITIZE_ADDRESS__`/`__SANITIZE_THREAD__`/`__has_feature(*_sanitizer)`; hits reported as a file:line checklist with a suggested `facts.key_flags` entry — recommended to the user, never silently written into committed config), runs the first gear-up build (⇒ first facts snapshot), writes the base openings (`freeplay`, gold-digger, recipe-derivation). Checkmate = proven-recipe count + published snapshot. Replaces crun's `/crun-mcp-init` and chess's manual seat/playbook ritual. |
| every request | `/arbiter-play <request>` | curator selects the matching opening — or `freeplay`, the generic plan → gear-up → orient → execute → verify loop, so *any* request can be played — and the refereed loop runs invisibly. |
| capture knowledge | `/playbook-create` | replaces skill-create: interview → draft → `AddPlayBook` (append-only); gotchas keep compounding via `NotePlaybook` during play. |

Beyond these four verbs the session is stock Claude Code — no recipe ceremony, no index commands, no seat management. `arbiter status`/`report` exist for spectating, not operating.

### MCP tools by seat (constructive RBAC: unregistered = nonexistent)

**player** (.mcp.json, session-resident, no credential) — 10 tools:
`ShowStepJob`, `CreateTask{request, fact_refs?≤8}`, `CheckStepJob`, `SubmitCheckpoint{decision}`, `ListTask`, `ReviewTask` (now incl. briefing, evidence, expect_report), `NotePlaybook`, `AddPlayBook`, plus proxied **`search`**, **`detail`** (cipher's exact two tools — the frozen surface, unchanged names/schemas; gold-digger already mandates fact-first hunting by the player).

**curator** (inline frontmatter, `ARBITER_SEAT_KEY`) — 4 tools: `ReadPlayBook`, `LoadPlayBook`, `ListTask`, `ReviewTask`.

**executor** (inline frontmatter, credential) — 8 base + 3 gated:
`SubmitTask{task_id, summary≤1024B, report, result:ResultSpec}`, `RegisterTest{paths}`, `ListTask`, `ReviewTask`, `search`, `detail`, `run{tests, options{harness_options.gtest.{fail_fast,timeout_s}}}`, `recipe_search{query}` (renamed from crun `search` — the only name collision); **`register`/`import_recipes`/`scan`** registered only when the loaded playbook declares `capabilities:[recipes]`. Edge semantics fail-closed: no active match at seat birth ⇒ gated tools not registered; every gated call re-checks under flock that the granting match is still current, else `capability_revoked`.

**Companion diagnostics (ADR-0010, bundled, not seats):** `gdb-mcp` and `perf-mcp` ship inside `arbiter-engine` as `arbiter_engine.gdbmcp`/`arbiter_engine.perfmcp` — delivering arbiter delivers them. They remain FOREIGN stdio servers in the ADR-0006 sense: `.mcp.json` launches them via the resolved engine interpreter (`python3 -m …`), never via the arbiter binary, so the deny-self guard is never in play. `arbiter init` probes the engine, add-if-missing merges the two entries, and writes the `arbiter-debugger` executor-agent variant wired with them. Executors use their tools for crash/perf evidence; adjudication consumes their `structuredContent` only through mcp-kind `expect[]` clauses. The seat RBAC boundary is untouched — companions are host-level capabilities like Bash, never arbiter tools.

### ResultSpec — the predicate language (SubmitTask, `[Verify]`, `[SetGoal]`)
```
{kind:"shell", command}                                          # escape hatch, unchanged
{kind:"mcp", server, tool, arguments,                            # FOREIGN servers only;
 expect?:[{path, op: eq|ne|ge|le|exists, value}]}                # self resolved → denied outright;
                                                                 # ≤8 clauses, scalars, closed ops
{kind:"run", recipe?, tests:[...], options?,
 expect:{overall: enum|one_of[...], max_failed?, min_passed?, test?:{name,result}}}
{kind:"fact", query:"<search mini-language>",
 expect:{min_results?, max_results?, complete?, reachable?, total_at_least?}}
+ timeout_s (default 600, max 3600), output_lines
```
Closed key sets; typed field comparison only; the per-clause `expect_report [{path,op,value,actual,ok}]` (graft from design A) is stored on the Task and surfaced via `ReviewTask`. Recursion guard, deny-by-default: any mcp-kind target whose resolved command (LookPath+Abs+EvalSymlinks) equals `os.Executable()` is rejected with `reserved_server` — full stop; the **only** sanctioned self-paths are run/fact kinds, which never go through `.mcp.json`. (Decision vs the winner's looser "tool-level guard": deny-self-entirely is strictly more precise, composes with the kind system, and ships with an adversarial test matrix — symlinks, renamed binaries, argv injection.) `[Verify]` blocks put named predicate specs in the playbook trust domain; executors invoke them by name (`verify:"repro-passes"`), closing the trivially-true-predicate hole for steps that declare verification.

### Engine-internal JSON-RPC methods (not tools; invisible to models)
`arbiter/refresh` (writer engine reconciles overlay; called by the referee before player-side fact predicates, deduped per round), `arbiter/census {scope}` (work-tree digest for memo/cache), `arbiter/resolveBriefing`, `arbiter/startRun` / `arbiter/runStatus` (async goal execution). New referee capability lands here, never on a model-facing surface.

---

## 6. The Agent Loop — Worked Example

Scenario: your gtest-guarded C DBMS (postgres-scale LOC); the user typed `/arbiter-play fix the deadlock the nightly run keeps hitting in the lock manager`; the curator loaded playbook `lockmgr-bugfix`. In round 1 the conventional gear-up step ran: the request named no instrumentation, so the player dispatched `{kind:"run", stage:"src_compile", profile:["debug"], expect:{overall:"passed", facts:{published:true}}}` — the shimmed build went green and published snapshot `s-42`, so every fact below is fresh by construction, with zero indexing ceremony. Now step `fix` says "eliminate the unlocked write to `PROCLOCK.holdMask` found in step *hunt*". The step declares:

```
[Verify]
- repro-passes: {kind:"run", recipe:"lockmgr_tests", tests:["DeadlockRepro.*"],
                 expect:{overall:"passed"}}
- no-unlocked-writers: {kind:"fact",
                 query:"writers:code:field:7c1a... file:src/backend/storage/lmgr/",
                 expect:{complete:true, max_results:1}}
```

1. **Player orients.** `ShowStepJob{}` → step `fix`, checklist, gotchas (one prior note: "holdMask also written via dispatch table in proc.c"). Player calls `search("writers:code:field:7c1a...")` → its QUERY engine answers from snapshot+overlay: 2 writers, `complete:true`, `view_state:"base"`. Data moved: read_index rows → byte-budgeted FactSummaries; `_meta:{match_id:"m-…", round:4}` stamped into the engine's facts.jsonl.
2. **Player dispatches.** `CreateTask{request:"Guard the holdMask write in LockReleaseAll with partitionLock; do not touch the dispatch path", fact_refs:["code:function:ab32...","code:field:7c1a..."], verify:"repro-passes"}` → referee calls `arbiter/resolveBriefing` → two `detail(budget=small)` cards (signatures, source spans, top callers; 3.1KB total) stored on Task T17. A bad ref would return `briefing_unresolved` — the task is never created with silent gaps.
3. **Executor grounds.** Subagent spawns; seat process verifies `ARBITER_SEAT_KEY`, eagerly spawns its QUERY engine, registers base tools (no `capabilities:[recipes]` in this playbook ⇒ no register/scan tools exist). `ReviewTask{task_id:"T17"}` returns request + briefing cards — compiler-grade orientation for the price of two IDs. Executor optionally runs `detail` on a caller, then edits `lock.c` with host tools.
4. **Executor builds and tests.** `run{tests:["DeadlockRepro.*"]}` → EXEC engine spawns; routing maps to recipe `lockmgr_tests`; cache check: stage key matches but the census over the recipe's `sources:["src/backend/storage/lmgr/**","src/test/modules/lockmgr/**"]` is dirty (lock.c edited) → forced rebuild under `locks/build/<h>.lock`; harness adapter `gtest` injects `--gtest_output=xml:.arbiter/runs/r-91/lockmgr_tests.xml`; result parsed from the XML only: 1 failed. `RunResult{overall:"failed", per_test:[...], guidance:[{test:"DeadlockRepro.Basic", next_queries:["detail code:function:ab32...","search \"callers:LockReleaseAll depth:2\""]}]}` — the red test hands the model its next fact queries (graft from design B). Executor fixes the off-by-one, reruns: `overall:"passed"`.
5. **Executor submits proof.** `SubmitTask{task_id:"T17", summary:"Guarded holdMask write; repro green", report:"...", result:{kind:"run", recipe:"lockmgr_tests", tests:["DeadlockRepro.*"], expect:{overall:"passed"}}}`. Phase 1 in-lock: spec validated, recipe hash checked against `Match.recipes_pin` (mismatch would fail with journaled `recipe_pin_mismatch`), round_seq recorded. Out-of-lock: referee executes via the seat's EXEC engine, gets `RunResult{overall:"passed", run_id:"r-92"}` with `match_id/task_id` written into the run row via `_meta`. Phase 2 re-lock: round_seq unchanged ⇒ verdict `pass`, `evidence:{run_id:"r-92", overall:"passed", passed:6, failed:0}`, `expect_report:[{path:"overall",op:"eq",value:"passed",actual:"passed",ok:true}]`.
6. **Player adjudicates.** `CheckStepJob{}` → all tasks pass → referee runs the step's `[Verify]` predicates from the **playbook trust domain**: `repro-passes` (memoized within the round from T17's identical spec? no — re-executed; cache makes it cheap), and `no-unlocked-writers` via the **player's** QUERY engine after `arbiter/refresh` (writer engine ingests the lock.c edit into the overlay) — result `{complete:true, result_count:1}` with `evidence:{snapshot_id, overlay_id, view_state:"overlay"}`. Both pass ⇒ success branch. The match has a run-kind `[SetGoal]` (`make check`-scale); referee starts it with `arbiter/startRun` and returns `{complete:false, reason:"goal_running", run_id:"r-93"}`; the player continues (Stop gate holds); a later `CheckStepJob` polls `arbiter/runStatus`, settles two-phase, and — goal passed — declares checkmate. With `goal_memo: true` and an unchanged census digest since a prior pass, the re-run would have been skipped and journaled `goal_checked{memoized:true}`.
7. **Learn.** Player `NotePlaybook{step_id:"fix", note:"holdMask writes must hold the partition lock; check proc.c dispatch path too"}` — appended into the committed playbook. `arbiter report m-…` later shows T17's two runs, the failing-then-passing repro, and the gotcha hit.

---

## 7. Synergies (component → tool → schema)

1. **Typed run predicates end the false checkmate.** `verify` (Go) → executor `SubmitTask{kind:"run"}` / playbook `[SetGoal]` → engine `run` → `RunResult.overall` compared structurally → `Result.evidence{run_id, overall, passed, failed, first_failure_name}`. Kills gold-digger's documented `isError=false`-on-failure trap and the hand-duplicated shell recipes.
2. **Census-based goal memoization** (default-off). `shared/census` → referee `arbiter/census` → digest = sha256(sorted(path,hash) over goal scope + toolchain_hash + goal-spec hash + recipes book hash) → `Match.goal_memo`. New/deleted files are detected by direct tree walk — the repro-file blindness of a snapshot-inventory digest is structurally impossible.
3. **Correct-polarity persistent build cache.** `shared/census` + recipe `sources:` globs → `runs/` cache table `{key, sources_digest, binary}` → hit requires clean census; no `sources:` ⇒ no cross-process hit. Fixes crun's stale-binary reuse *and* cache-lost-on-restart in one mechanism.
4. **Build-driven indexing.** `runs/` `src_compile` → `shared/interpose` cc-shim → compile-db journal + extraction queue → `facts/` snapshot publish, all inside one run verdict (`facts:{published, snapshot_id, …}`). The build the agent needed anyway *is* the index: cipher stops being build-blind, the compile database is always the actually-built TU set, and the user never runs an index command.
5. **Fact briefings.** player `CreateTask{fact_refs}` → `arbiter/resolveBriefing` → `detail(budget=small)` under cipher's degradation ladder → `Task.briefing[]` → executor `ReviewTask`. Ground truth pushed into executor context for the cost of IDs.
6. **Fact predicates machine-ground checklist items.** playbook `[Verify]` → `{kind:"fact", query, expect{complete, max_results,…}}` → `search` structured fields (`complete`, `total_is_exact`, `reachable`) → adjudication-grade structural proofs ("no writers outside the lock module, proven, not budget-truncated").
7. **Red-test→facts loop closure.** `runs/` adapter failure path → `RunResult.guidance[]` (failing symbols looked up in `facts/read_index`, copy-paste `search`/`detail` strings, file:line) → executor's next action is handed to it at the moment of failure.
8. **Adjudicated recipe authoring** (graft from design B). `capabilities:[recipes]` playbook → executor `register` → SubmitTask `{kind:"run", recipe:<new id>, expect:{overall:one_of["passed","failed"]}}` proves compile+launch+structured-output → recipe enters the committed book only after a refereed proof; `[SetGoal]` checkmates on proven counts.
9. **Correlated telemetry.** `engineclient` `_meta` stamping → run/run_test rows + all log channels carry `{match_id, round, task_id, run_id}` → `arbiter report` joins journal with runs SQLite — the substrate for gotcha curation and match-archive search.
10. **One init, one entry, resident engines.** `deploy` collapses the 5-step manual deployment to one command (executor agent auto-written, key injected); per-seat resident engine children end spawn-per-predicate cold starts without daemons.
11. **Profile-from-request, facts held constant.** player gear-up → `run{stage:"src_compile", profile:["asan"]}` → recipe `profiles:` overlay applied; build cache keys *include* profile, extraction cache keys *exclude* codegen flags → instrumented binaries on demand without ever re-indexing; the chosen profile lands on the run row for audit and in goal-memo digests for correctness.

---

## 8. Language & Packaging Strategy

**Polyglot with one Go entrypoint and one installed Python engine; exactly one rewrite (crun's surface layer).**

1. **The fact extractor cannot leave Python:** cipher's red lines mandate ctypes libclang and zero PyPI deps; its ~1000-LOC binding and conformance corpus are the bundle's crown jewels, and the approved native-worker roadmap assumes the Python reference path.
2. **The referee cannot leave Go:** race-tested two-phase locks, process-group kills, ms-startup static-binary seats and the per-Stop-event hook are the product's spine; a Python port re-verifies every concurrency property for negative gain.
3. **The seam is one protocol:** stdio JSON-RPC. Go side: a minimal hand-rolled `engineclient` (the vendored go-sdk cannot issue custom methods and its jsonrpc2 is `internal/`; against cipher's deliberately minimal hand-rolled server, a matching minimal client is the smallest honest option — decision over key-gated tools, to keep model-facing surfaces frozen). Golden stdio-transcript contract tests (recorded exchanges replayed against both runtimes in CI) gate every PR — the permanent defense against silent adjudication drift.
4. **crun is the right rewrite:** smallest codebase, hermetic fake-harness tests port over, and its dependency stack is the only obstacle to a pure-stdlib engine. An AST meta-test (crun's own `no-import-re` pattern, generalized) enforces the stdlib-only import boundary as a failing test, not policy.
5. **Why the engine does not move to Go even though crun is being rebuilt:** the rebuild is crun's *surface* — FastMCP→stdlib loop, pydantic→dataclasses, PyYAML→subset parser — while its semantics, state schema, and hermetic test suite port as Python; cipher ships verbatim, and rewriting it would re-verify its conformance corpus from scratch with silent fact-divergence (an adjudication bug class) as the failure mode. The engine's hot paths are already native — clang's parser, SQLite, sha256, and the build's own child processes — so Go buys speed only on a thin glue slice that the budget ladder already caps. ctypes is load-bearing, not legacy: it dlopens **whatever libclang the host toolchain provides, located at runtime** (clang_executable → llvm-config → platform paths); cgo structurally cannot match that without either pinning a libclang at build time (breaking version-agnostic portability on internal/offline machines) or forfeiting the static-single-binary property chess prizes. The two real Python costs are handled surgically instead: the per-TU interposition hop lives in the Go binary (`arbiter cc`), and per-TU extraction speed follows cipher's approved native-worker roadmap inside `facts/`, with Python remaining the conductor and reference implementation. Escape hatch: if post-merge profiling ever shows engine *glue* (not clang/SQLite) dominating a measured budget, the JSON-RPC seam plus golden transcripts make a leaf-by-leaf port safe later — the transcripts pin behavior, so a future port is a refactor, not a re-verification.
6. **Distribution:** `arbiter` Go binary (vendored, offline build) + `pip/uv install arbiter-engine` (zero runtime deps; `[scan]` extra pulls tree-sitter for cold-repo gtest discovery only). `engines.json` pins the interpreter machine-locally with typed staleness errors surfaced in `arbiter status`. Opt-in `--embedded-engine` mode (air-gapped hosts) unpacks via go:embed into `.arbiter/engine/` **with sha256 verification against the embedded digest at every spawn, journaled, plus Edit/Write deny rules on the tree** — the adjudication evaluator is never silently model-patchable.

Rejected: all-Go (rewrites the conformance-tested extractor against a red line, and trades ctypes' runtime-located libclang for cgo's build-time pinning), all-Python (loses the referee's properties), daemon broker (violates no-daemon; per-seat children achieve pooling within existing lifetimes), conventions-only status quo (leaves false checkmate, cold starts, three inits, zero data flow).

---

## 9. Performance Notes

- **Cold index = first gear-up build:** extraction overlaps the build via the interposed queue (bounded pool, `cores/4` while compilers run, full width after). A libclang parse is front-end-only work — generally cheaper than the same TU's `-O2` compile — so on a parallel build most extraction hides inside the build's own wall time, and the remainder is a measured publish tail (`facts:{extract_ms, hidden_ms, tail_ms}` on every src_compile verdict — the contract is observable, never asserted). cipher's ENDGAME items (worker cap to 32, extraction cache, single-pass snapshot+index write, per-file timeout wiring) all shorten the tail and continue inside `facts/`.
- **Profile switches cost zero facts work:** asan/coverage/debug rebuilds re-key the build cache but hit the extraction cache 100% by construction (semantic-flag keying); only feature-selection changes re-extract, and only their include-closure cone.
- **Incremental:** overlay reconcile is lazy and writer-gated; `arbiter/refresh` is referee-triggered, **deduped per round**, and pays only dirty-TU re-extraction; module gates inherited (publish p95 <10ms, single-TU ≤2s). Executor engines never pay reconcile.
- **Run economics:** census-validated disk cache survives restarts; cross-target identical `src_compile` builds once; build locks serialize same-workdir stages (correctness over parallel-build throughput). Goal runs are async (startRun/poll), so `CheckStepJob` never hits host MCP tool-call timeouts even for `make check`-scale goals; memoization (opt-in) removes repeat goal runs for unchanged trees. Census cost on postgres ≈ one stat-walk over the scope globs (mtime/size prefilter, sha256 confirm only on suspects) — tens of ms warm.
- **Token economics:** detail hard-capped 8/32/128KB with the staged degradation ladder (the bundle-wide response shaper); search previews ≤8 fields/128 chars; briefings ≤8KB/task and pruned from archived rounds; `ListTask` summaries ≤1024B; `ShowStepJob` slim by design; run artifacts returned as paths, `stdout_tail` capped; `guidance[]` ≤4 entries of copy-paste queries. The player's session surface is 9 tools, one `.mcp.json` server.
- **Predicate latency:** resident engine children amortize cold start (interpreter + SQLite open) to once per seat lifetime; the QUERY/EXEC split keeps fact queries sub-100ms even during builds.

---

## 10. Phased Migration (each phase independently shippable)

**Phase 1 — Bridge (existing three repos; weeks).** chess vN+1: `run`/`fact` ResultSpec kinds **with the final schema** (recipe/tests/query/expect fields — evaluation temporarily calls the existing crun-mcp/cipher-2 servers from `.mcp.json`, so phase-3 swaps the transport, never the meaning); `expect[]` clauses on mcp-kind; deny-self `reserved_server` + adversarial test matrix; recipe-book hash pinning at LoadPlayBook; exact-command Stop-hook claim; curator ListTask fix. crun vN+1: WAL/busy_timeout/`BEGIN IMMEDIATE`, `.c` scanner suffix, `run_test` occurrence column, `sources:` field + census-validated disk cache, `harness_options.gtest.*` namespacing. *Exit:* gold-digger rewritten on run-kind predicates; a failing gtest run cannot checkmate; 8-executor contention test green.

**Phase 2 — One init (still three repos).** chess deploy grows `--bundle`: writes all three `.mcp.json` entries, unions gitignore/deny rules, auto-writes the executor agent with key injected. *Exit:* a fresh checkout of the target DBMS deployed with one command (vs the documented 5 steps).

**Phase 3 — The merge.** Preconditions (owner-signed): decisions.md entries for every cipher delta (root paths, config nesting, `_meta` handling, shared census factoring, lazy reconcile, multi-namespace hosting); ENDGAME sync gate — in-flight WS items land or are explicitly parked, then cipher-2 is imported via git subtree and **frozen for new features**, with ENDGAME continuing inside `facts/` (decision: one-way import beats indefinite dual-tree divergence). Build: arbiter repo (Go = chess renamed + engineclient + deploy; engine = facts/ verbatim + runs/ stdlib rebuild + shared/), `.arbiter/` layout, eager spawn + lazy reconcile, single-writer facts, async goals, `arbiter adopt`. *Exit:* chess's race suite, cipher's 74 test files, crun's ported 42 tests, and the golden transcripts all green under the new layout; `adopt` exercised on both sibling checkouts.

**Phase 4 — The endgame loop.** Build-driven indexing: the `arbiter cc` shim (adversarial argv test matrix first), extraction queue + publish barrier, semantic-key extraction cache, `facts:{…}` on src_compile verdicts; recipe `profiles:`; `/arbiter-intro` as an adjudicated bootstrap match; fact briefings; `guidance[]`; `[Verify]` + capability gating; census memoization (ships default-off); `arbiter report`. gtest remains the only first-class harness — the real target is gtest-guarded. Each item lands as a README-spec'd, TDD'd PR through the implementer/reviewer relay. *Exit:* on the real DBMS repo, fresh clone → `arbiter init` → `/arbiter-intro` → `/arbiter-play <bug report>` completes a refereed fix with **zero** manual index or recipe steps; an asan-profile match reuses the plain-profile facts snapshot with zero re-extraction; first-build `tail_ms` is within the agreed budget.

**Phase 5 — Polish & compounding.** Openings library (gold-digger, recipe-derivation, regression-triage — knowledge/-derived); optional harness adapters (`exitcode`, `pgregress`, `tap`) validated against the dummy pg/sqlite checkouts, each behind an owner-signed result-grammar decision; opt-in embedded-engine mode with digest verification; match-archive search over correlated logs; continued ENDGAME performance work. Phases 1–2 deliver the bulk of the quality win before any code merges — the program's risk is deliberately back-loaded behind already-shipped value.

---

## 11. Risks & Mitigations

- **Program size for a part-time solo maintainer** (the judges' sharpest split): honestly, phases 3–5 are months of work. Mitigation is structural — phases 1–2 ship the false-checkmate fix and one-command deployment on the existing repos, so the merge can stall without losing the core value; phase 3 is gated on owner sign-off, not assumed.
- **Referee semantics regression:** new kinds land behind the existing `Pass()` seam; lock protocols untouched; chess's race tests ported first; async-goal settle reuses the existing two-phase/round_seq machinery.
- **Cross-runtime adjudication drift:** golden stdio transcripts are load-bearing CI, gating both repos' PRs forever.
- **Shim fail-open vs index fail-closed:** the `arbiter cc` shim must never break a build (exec-through on any internal error, journaled miss) yet publication must stay honest — a shim miss surfaces as `facts:{published:false}` or `warnings` on the same verdict, so the gear-up predicate fails closed instead of adjudicating against a silently partial index. Adversarial test matrix before it ships: response files (`@file`), ccache/distcc already stacked in `CC`, multi-arch flags, `make -jN` races, interrupted builds.
- **Extraction tail on the very first build:** worst case (few cores, huge unity-build TUs) the publish barrier adds real wall time after build-green. Mitigations: the pool widens once compilers go idle, the extraction cache persists machine-locally across clones, cipher's ENDGAME items shorten parse cost, and the tail is measured (`tail_ms`) and journaled — never hidden. If a repo's tail proves unacceptable, `facts:{published:"pending"}` + a follow-up fact predicate is the documented escape hatch, an explicit playbook choice rather than a silent default.
- **Instrumentation-macro blind spot:** facts index the allowlist-cleaned parse, so code gated on `__SANITIZE_ADDRESS__` / `__has_feature(address_sanitizer)` / `__OPTIMIZE__` is indexed in its plain configuration — the risky pattern is ASan-conditional allocator paths, locking variants, or struct fields (call edges and `has_field` facts that differ from the binary under test). Detection is automated: `/arbiter-intro`'s whole-token scan (same mechanism as `adopt`'s legacy-name checklist — no string-pattern *inference*, consistent with cipher's red line) reports every hit as file:line and recommends a `facts.key_flags` entry; the user confirms before it lands in committed config, since facts-relevance is a semantic judgment no run can prove. Opting in re-admits those flags into the extraction key, paying re-extraction on profile switches for exactly the affected TUs. Surfaced in `arbiter status`; hits confined to test scaffolding (the sqlite `test1.c` pattern) are typically declined. Validation note: the postgres checkout has zero hits (its instrumentation hangs off `USE_VALGRIND`, a `-D` define already in the key) and sqlite has one benign test-harness hit — the blind spot is real but rare, which is why detect-and-recommend beats key-by-default.
- **Harness adapters sit near the banned boundary (now optional, phase-5):** pg_regress/TAP parsing is restricted to *machine-generated result files in declared, version-pinned formats*, whole-token grammars, unknown lines fail closed; each adapter requires an owner-signed decisions.md entry. The `exitcode` adapter honestly caps sqlite (TCL/TH3) checkmates at target granularity — accepted, never faked. None of this is on the critical path: the real target is gtest-guarded.
- **Journal forensics vs redaction:** decision — redaction is per-channel; facts/runs channels keep cipher/crun's strict rules, but `journal.jsonl` retains chess's full-fidelity logging (0600, gitignored) because the bypass-cost story depends on fully-logged evidence; this is documented as the one deliberate redaction exception.
- **Memoization/cache conservativeness:** census-based digests detect new/deleted files by construction; digests fold in toolchain, goal-spec, and recipe hashes; memoization ships default-off until a property-test suite (cached-vs-forced equivalence, adversarial new-file cases) proves it; recipes without `sources:` never cache cross-process.
- **N engine children on one SQLite/facts store:** WAL + lock inventory + single-writer facts rule; the parallel-executor claim stays "DB-safe, build-serialized" until 8+ fan-out contention tests pass; NFS detected and refused at init.
- **Bespoke YAML-subset parser for recipe books:** fail-closed dialect, line-precise errors, crun's full recipe corpus as golden tests — accepted as the permanent price of the stdlib-only engine.
- **expect-language scope creep:** ops frozen at `eq/ne/ge/le/exists`, scalars only, ≤8 clauses, no wildcards/string ops — constitutionally documented; new needs go into the target tool's structured output.
- **Red-line renegotiations** (explicit, phase-3 blockers): zero-PyPI becomes "engine core stdlib-only + fail-closed `[scan]` extra"; "no third MCP tool" holds for the facts namespace while the engine process hosts other namespaces and referee-only methods; chess's 10 interfaces become per-seat 9/4/7–10. Each requires a decisions.md entry and owner sign-off.
- **Recipe `env` credential smuggling** (amplified by one-command init): `register`/`import_recipes`/init lint env values against secret-shaped names and warn.
- **Migration cutover:** adopt deletes derived state by contract; committed playbooks/agents referencing legacy tool names get a generated whole-token-scan checklist for manual rewrite (the constitution forbids automated prose edits) — a documented human step, not a silent gap.
- **Doc surface tripling in two languages:** module READMEs stay Chinese and spec-authoritative; unified top-level docs are English; per-phase doc-reconciliation passes are budgeted, and the reviewer-relay's "no silently dropped follow-ups" rule is adopted repo-wide.

---

## 12. Deliberately Dropped

- **FastMCP, pydantic, PyYAML as runtime deps** — the engine is stdlib-only so it merges with cipher and stays trivially vendorable; tree-sitter survives only as the optional `[scan]` extra, behind facts-derived gtest discovery, fail-closed when absent.
- **go:embed engine as the default distribution** — demoted to opt-in offline mode with mandatory spawn-time digest verification; the default is an installed package outside the repo, because a model-editable adjudication evaluator is the one regression the whole design exists to prevent.
- **crun's uvx/git-ssh launcher, `crun-mcp.toml`, and cipher's `python -c` sys.executable `.mcp.json` entry** — replaced by one Go-binary entry plus machine-local `engines.json`.
- **chess's path-equality `reserved_server` guard** — replaced by deny-self-outright for mcp-kind plus the run/fact kinds as the only sanctioned self-paths; also the trailing-words Stop-hook heuristic (exact-command claim) and the dead `AbortReplaced`/`AbortInternalError` constants.
- **The standalone /crun-mcp-init skill and per-project skill sprawl** — recipe derivation becomes the `/arbiter-intro` adjudicated bootstrap match; daily use is exactly four verbs (`arbiter init`, `/arbiter-intro`, `/arbiter-play`, `/playbook-create`).
- **cipher's standalone init/rebuild ceremony as a user step** — the gear-up build owns cold and incremental indexing end-to-end; `arbiter index` survives only as CI/recovery plumbing.
- **pg_regress/TAP adapters as core scope** — the real target DBMS is gtest-guarded; the pg/sqlite checkouts are validation dummies, and their adapters are optional phase-5 work behind owner-signed grammar decisions.
- **cipher's background poll thread and `overlay_ttl_seconds`** — freshness is lazy reconcile plus deterministic referee-triggered refresh; no file watchers.
- **`knowledge/` as runtime content** — authoring source for the openings library only; FACT-only preserved.
- **crun's `audit.jsonl` as a separate trace format and the three separate log schemas** — one channel family, per-channel redaction, shared correlation IDs.
- **Per-bug-type shell oracle duplication in playbooks** — subsumed by run-kind `expect` variants against adapter results.
- **Three inits, three `.mcp.json` entries, three state dirs** — one deploy package; legacy dirs retire via `arbiter adopt`.
- **Graph projection / inference / impact / Concept / public relations tools** — cipher's deleted layers stay deleted; the only new fact-side capability is the referee consuming the existing search surface as predicates.
- **HTTP MCP, daemons, Windows, multi-match concurrency, derived-state format compatibility, embedding/LLM indexing** — all three projects' non-goals inherited verbatim; the unified product does not relitigate them.
