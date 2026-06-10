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

## Base openings (shipped by intro; committed)
- **freeplay** — the generic loop: gear-up → orient (fact queries proportional to request
  scope) → plan (CreateTask per work item, fact_refs attached) → execute/verify → learn.
  `[SetGoal]` defaults to the repo's primary suite recipe once one is proven.
- **gold-digger** (rewrite of the legacy playbook) — bug hunt on typed predicates: repro task
  (`expect:{overall:"failed", test:{name,result:"failed"}}` — proving the bug exists), fix
  steps with `[Verify] repro-passes` + fact predicates (e.g. `writers:` bounds), goal = suite
  green. The false-checkmate regression test mirrors this opening.
- **recipe-derivation** — the capability-gated (`capabilities:[recipes]`) opening intro uses;
  also runnable later for new targets.
- (M8) **regression-triage** and knowledge/-derived openings.

## Design rules for openings (binding for playbook-create output)
Steps small and checkable; every step with externally-visible effect declares `[Verify]`;
checklists are fact- or run-groundable wherever possible; gotchas are one-line, step-scoped,
append-only; no prose that asks the model to self-assess success — that's the referee's job.

## Tests
Skill smoke tests are scenario-level (reviewer-held acceptance suite). In-repo: template lint
(every generated playbook parses; gear-up step present; [Verify] names resolve), intro's macro
scan against fixtures (postgres-like: zero hits; sqlite-like: test-scaffold hit → checklist
declines by default), opening fixtures parse + load under the referee grammar.

## Done
M7 (the four verbs end-to-end on fixtures) → M8 (openings library growth).
