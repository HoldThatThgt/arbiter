# Milestones

Build order for this repo. Each milestone ends green and demo-able; no milestone depends on a
later one. **[OWNER GATE]** steps are performed by the owner (subtree imports of the source
repos), after which the implementer extends in place. The bridge phases described in
`design.md §10` (landing typed predicates on the *existing* three repos first) are run by the
owner in those repos and are out of scope here; this repo is the merge endpoint.

## M0 — Scaffold & CI
Repo layout (`cmd/arbiter`, `internal/`, `engine/arbiter_engine/`, `testdata/`), Makefile,
CI: `go vet` + `go test -race`, Python `unittest`, the stdlib-import AST meta-test (fails on
any non-stdlib import in `engine/`), golden-transcript harness skeleton (record + replay tools,
empty corpus), lint config. **Exit:** CI green on an empty walking skeleton; transcript replay
runs against a hello-world engine stub from both runtimes.

## M1 — Go core port-in
**[OWNER GATE]** subtree-import chess → `internal/{match,verify,playbook,seat,journal,deploy}`,
`cmd/`. Rename chess→arbiter (binary, env vars, state paths `.chess/`→`.arbiter/match/`),
delete dead constants, fix curator ListTask whitelist drift. **Exit:** chess's full test suite
(incl. race suite) green under the new name and paths; `arbiter serve player` speaks MCP.

## M2 — Engine core + engineclient
`engine/`: line-delimited JSON-RPC stdio loop (from cipher's server pattern), namespace router,
`_meta` extraction, typed error taxonomy, config.yml strict YAML-subset parser, logging channels.
Go: `internal/engineclient` (~300 LOC), spawn/reap lifecycle (Setpgid, kill-group, stdin-EOF
self-exit — darwin-tested). Golden transcript corpus v1 covering every method/tool stub.
**Exit:** seat spawns engine child; tools/list forwarded live; transcripts replay green on both
runtimes; kill -9 of a seat leaves no orphan engine (verified on darwin).

## M3 — Typed predicates end-to-end
`internal/verify`: `run`/`fact` ResultSpec kinds with final schemas; `expect[]` on mcp-kind;
deny-self `reserved_server` + adversarial matrix; recipe-book pinning at LoadPlayBook;
`Task.briefing[]`, `Result.evidence`, `expect_report[]` on match state; async goal scaffolding
(`startRun`/`runStatus` against stub engine). Engine: stub evaluators returning canned
structured results (real ones land M4/M5). **Exit:** the false-checkmate kill test — a stub run
returning `{overall:"failed", isError:false}` can NOT pass a `{expect:{overall:"passed"}}`
predicate; gold-digger rewritten on typed predicates against stubs.

## M4 — Facts absorption
**[OWNER GATE]** subtree-import cipher-2 → `engine/arbiter_engine/facts/` (verbatim; ENDGAME
work continues here; source repo frozen for new features per design §10 phase 3). Owner-signed
deltas only: `.arbiter/facts/` paths, config nesting, `_meta` handling, lazy + writer-gated
overlay reconcile, inventory hashing factored to `shared/census`, multi-namespace hosting.
`arbiter/refresh` + single-writer rule + `view_state` evidence. **Exit:** cipher's 74 test files
green in the new layout; `search`/`detail` transcripts byte-identical to cipher-2's responses
on the conformance corpus; fact-kind predicates adjudicate real queries.

## M5 — Runs rebuild (gtest-first)
`engine/arbiter_engine/runs/`: RecipeBook v2 parser (vars, profiles, compile_db, targets with
harness/sources/stages), proven/unproven lifecycle on SQLite (WAL, `BEGIN IMMEDIATE`,
correlation columns, `run_test.occurrence`), runner with pre/cmd/post stages, **gtest adapter**
(injected `--gtest_output`, XML primary), census-validated build cache (correct polarity,
cross-process under `build/<h>.lock`), `run`/`recipe_search`/`register`/`import_recipes` tools,
`guidance[]` on failure (read_index lookups), facts-derived TestBody discovery (tree-sitter
demoted to `[scan]` extra, fail-closed). Port crun's fake-harness test corpus. **Exit:** crun's
ported tests green; run-kind predicates adjudicate real fixture runs; stale-binary polarity test
(edit → cache must miss) green; 8-way contention test green or claim downgraded per ADR-0009.

## M6 — Build-driven indexing
`arbiter cc` (Go, `internal/interpose`): journal argv+cwd, enqueue, exec-through; adversarial
argv matrix FIRST. Engine `shared/`: journal consumer → compile-db → `CodeFactExtractor.collect`
over the compiled TU set (bounded extraction pool: cores/4 during build, full width after) →
`FileFactStore.replace_snapshot` (content-addressed; the profile is part of each source id, so a
sanitizer profile publishes its own snapshot), `facts:{published, snapshot_id, extract_ms,
hidden_ms, tail_ms}` on src_compile verdicts, `profiles:` overlays, typed `no_snapshot` error.
**Exit:** on a fixture repo: clean build publishes a snapshot inside the run verdict; asan-profile
rebuild publishes its OWN content-addressed snapshot (distinct id, not the plain build's);
shim-miss / non-green show `facts:{published:false}` failing the gear-up predicate closed, while an
incapable toolchain instead hard-stops the run as `failure:indexer_unavailable` (mandatory index,
ADR-0020).

## M7 — Deploy, seats, skills: the four verbs
`internal/deploy` rewrite: `arbiter init` (engines.json + verification, seat key, curator AND
executor agents auto-written/key-injected, skills, structured merges, exact-command Stop-hook
claim, NFS refusal, `--remove`), `arbiter adopt` (legacy-name whole-token checklist). Skills:
`/arbiter-play` (opening selection + freeplay), `/arbiter-intro` (adjudicated bootstrap: build
probe → recipe derivation+proof → shim install → instrumentation-macro scan → first gear-up →
base openings), `/playbook-create` (gear-up step templated). Briefings (`fact_refs` →
`resolveBriefing`), `[Verify]` named predicates, capability gating, goal memoization
(default-off), `arbiter report`, compose-on-read `arbiter status`. **Exit:** the endgame demo —
fresh fixture clone → `arbiter init` → `/arbiter-intro` → `/arbiter-play <bug report>` completes
a refereed fix with zero manual index/recipe steps.

## M8 — Hardening & polish
Full contention runs at 8+ executor fan-out; darwin reaping torture tests; openings library
(gold-digger, recipe-derivation, regression-triage); embedded-engine opt-in mode with digest
verification; match-archive search over correlated logs; optional harness adapters
(exitcode/pgregress/tap) each behind its own ADR; doc reconciliation pass.
**Exit:** acceptance scenario suite (reviewer-held) green; performance budgets recorded in
design §9 confirmed on the dummy DBMS checkouts.
