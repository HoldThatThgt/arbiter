# skills-and-playbooks — `.claude/skills/*`, the openings library

## Identity
The product's face: the four user verbs and the committed playbooks ("openings") that make the
refereed loop invisible. Skills are deployed by go-deploy; openings live in
`.arbiter/playbook/` (committed). cipher's `knowledge/` corpus is authoring source material for
openings, never runtime content.

## The four verbs
1. **`arbiter init`** (shell) — go-deploy. Wiring only.
2. **`/arbiter-intro`** (once, in CC) — an **adjudicated bootstrap match** (not a script):
   a. probe the build system (make/cmake/custom; locate toolchain);
   b. derive candidate recipes and **prove** them — each enters the committed book only after a
      refereed `{kind:"run", expect:{overall:one_of[passed,failed]}}` task demonstrates
      compile + launch + structured gtest output;
   c. install `arbiter cc` interposition into proven `src_compile` stages;
   d. run the **instrumentation-macro scan**: whole-token grep for `__SANITIZE_ADDRESS__`,
      `__SANITIZE_THREAD__`, `__has_feature(*_sanitizer)`; report hits as a file:line checklist
      with a suggested `facts.key_flags` entry — recommended to the user, NEVER silently written
      to committed config (facts-relevance is a semantic judgment no run can prove);
   e. first gear-up build ⇒ first facts snapshot;
   f. write the base openings (below).
   Checkmate: proven-recipe count + published snapshot. Gotchas discovered during intro land in
   the intro playbook itself — the bootstrap learns too.
3. **`/arbiter-play <request>`** (every request) — select the opening whose declared intent
   matches the request; fall back to **freeplay** so every request is playable. The skill tells
   the player its constitution: analyze/dispatch only, fact-first orientation, derive the build
   profile from the request (asan ⇐ memory corruption; coverage ⇐ test-gap work; debug
   otherwise; feature flags the request names), never attempt to bypass the referee.
4. **`/playbook-create`** (capture knowledge — where skill-create used to be) — interview →
   draft → `AddPlayBook` (append-only). The template ALWAYS emits step 1 = gear-up (typed
   src_compile predicate with `facts:{published:true}`), and encourages `[Verify]` named
   predicates + a run-kind or fact-kind `[SetGoal]`.

## Starter openings (ADR-0012; embedded in the binary, refreshed to the shipped version on every `arbiter init`)
The shipped openings are **not** write-if-missing: every `arbiter init` re-writes them to the
current shipped template (atomicWrite), so upgrading arbiter and re-running init delivers the new
versions — to customize one, fork it to a new name via `AddPlayBook` (your own-named books are
never touched by init).
Repo-agnostic, referee-native, convention-linted in CI:
- **fix-reported-bug** — known misbehavior: a test-author writes a deterministic repro that
  asserts CORRECT behavior (red while the bug lives) and RegisterTest-freezes it. Two plain
  `src_compile` run predicates gate the loop: `repro-runs-red` (expect `overall:failed`) and
  `suite-green` (expect `overall:passed`, `max_failed:0`). The fix is accepted only when the
  frozen repro flips to green with the suite intact; untouchability comes from the
  RegisterTest freeze (the referee re-hashes the test before the verdict), not from a diff
  check.
- **hunt-latent-bugs** — unknown defects: ONE falsifiable hypothesis per round, SYMPTOM-test
  polarity (test passes iff bug exists ⇒ `<build> && <run>` exit 0 == bug machine-proven),
  watchpoint strengthening, reachability qualification, anti-loop history discipline.
- **build-feature** — scenario-first TDD: `build && ! run` proves red-for-the-right-reason,
  user approval gates, test untouchability as a predicate clause at every later step.
- **fix-slow-path** — measured perf: a test-author writes a frozen COMPLEXITY-RATIO test —
  workload at N and 2N, asserting `time(2N)/time(N)` stays under a fixed bound K — gated by the
  same two plain `src_compile` run predicates (`ratio-runs-red` expect `overall:failed`,
  `suite-green` expect `overall:passed`/`max_failed:0`). perf-mcp's noise-band measurement
  (`perf.measure_command`, double baseline) is analysis-only — it finds WHERE to optimize; the
  verdict is the frozen ratio test moving toward green, never the measurement.

## Intro-authored openings (M7; committed, repo-specific)
- **freeplay** — the generic loop: gear-up → orient → plan → execute/verify → learn.
- **gold-digger** (rewrite of the legacy playbook on run/fact predicates) — repro task
  `expect:{overall:"failed", test:{name,result:"failed"}}`, `[Verify] repro-passes` + fact
  predicates, goal = suite green. The false-checkmate regression test mirrors this opening.
- **recipe-derivation** — the capability-gated (`capabilities:[recipes]`) opening intro uses.
- (M8) **regression-triage** and knowledge/-derived openings.

## Naming & predicate conventions (ADR-0012; binding for every opening and playbook-create output)
Name = user intent as an imperative phrase (verb-first kebab-case, ≤3 segments, file stem ==
name). Description leads "Use when …" and cross-points "Do not use … (use <other>)" wherever
intents are adjacent — dedup at curator-selection time. Steps state the EXACT result predicate
(shell with explicit polarity, or mcp+`expect`); laws are machine checks inside predicates,
never prose; checklists mechanically checkable; gotchas one-line, step-scoped, append-only; no
prose asking the model to self-assess success — that's the referee's job.
`TestEmbeddedOpeningsParseAndFollowConvention` (go-deploy) lints the shipped set.

## Companion diagnostics in openings (ADR-0010)
Openings may direct executors to the companion servers — bundled with arbiter-engine and wired
by deploy whenever the engine resolves:
**gdb-mcp** for crash/state evidence (run the repro under GDB, `gdb_snapshot` at the stop,
watchpoints to catch the corrupting write) and **perf-mcp** for performance evidence
(`perf.scan_c` findings, `perf.measure_command` before/after medians). Two rules hold:
companion evidence is *diagnostic input* to hypotheses and reports, and whatever a step
adjudicates on stays typed — mcp-kind predicates with `expect[]` clauses over the companions'
`structuredContent` fields (e.g. `summary.all_successful`, `state`, `summary.finding_count`),
never their text summaries. Steps phrase companion use conditionally ("when gdb-mcp is wired")
so openings stay playable on hosts without the companions; the `arbiter-debugger` agent is the
preferred dispatch target for these tasks when it exists.

## Tests
Skill smoke tests are scenario-level (reviewer-held acceptance suite). In-repo: template lint
(every generated playbook parses; gear-up step present; [Verify] names resolve), intro's macro
scan against fixtures (postgres-like: zero hits; sqlite-like: test-scaffold hit → checklist
declines by default), opening fixtures parse + load under the referee grammar.

## Done
M7 (the four verbs end-to-end on fixtures) → M8 (openings library growth).
