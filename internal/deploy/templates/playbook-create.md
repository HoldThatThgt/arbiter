---
name: playbook-create
description: Interview the user and register a new Arbiter opening.
---

Create a playbook by interviewing first, then drafting, then calling AddPlayBook.

## Protocol

1. Interview in ONE pass — ask for everything missing at once: intended request
   class, steps, failure branches, machine-checkable predicates per step, primary
   recipe or fact goal, required capabilities, round budget (max_steps).
2. Draft the full playbook text against the contract below and the scaffold.
3. Call AddPlayBook {"content": "<the full markdown>"} and handle its errors:
   - playbook_invalid → fix exactly what data.issues lists, resubmit;
   - name_conflict → the intent already has an opening: re-read the existing names,
     either extend that opening (tell the user) or pick a genuinely different intent
     phrase — never overwrite.
4. After success, report: the name, the step graph (step → success/failure targets),
   each step's predicate, the goal, max_steps — and that the user can run it now
   with /arbiter-play.

Every generated playbook must follow this contract:

- Naming (ADR-0012, FORMAT.md "Naming & dedup"): `name` is the USER INTENT as
  an imperative phrase — verb-first, kebab-case, ≤3 segments (fix-reported-bug,
  build-feature) — never the method, mechanism, or a codename. `description`
  leads with "Use when …" and cross-points "Do not use … (use <other>)" when
  another opening's intent is adjacent. Check the existing names first: if the
  intent overlaps, extend that opening instead of forking a near-copy; on
  name_conflict pick a new intent phrase, never overwrite.
- Step 1 is always `gear-up`.
- `gear-up` uses a typed `src_compile` run predicate named `gear-up-published`.
- Steps with external effects should declare named `[Verify]` predicates.
- Every checkable step states the EXACT predicate the executor must submit —
  shell with explicit exit-code polarity, mcp + `expect` clauses, a typed
  run/fact spec, or a curated `[Verify]` name. Encode laws as machine checks
  (test untouchability = `git diff --quiet -- <paths> && …`, determinism = a
  5x loop, measured gain = expect-clause measurements vs a recorded noise band).
- Any checklist line that says "Submit X" in prose MUST be paired with a
  `[Submit] X` binding line on the step (placed after `[CheckList]`, before
  `[Branch]`), where `X` is a curated `[Verify]` name. An unbound prose
  "Submit X" can be gamed by any trivially-true predicate (FORMAT.md:102-111);
  the binding line forbids substituting a weaker or inline spec.
- Checklists must be fact- or run-groundable; never ask a model to decide success.
- Gotchas are one-line, step-scoped, append-only notes.

Use this scaffold and adapt only the names, descriptions, branches, predicates, and task text:

```markdown
---
name: new-opening
description: One sentence describing the request class this opening handles.
max_steps: 32
verify_policy: named
---

[Verify] gear-up-published
run: src_compile
tests: ["src_compile"]
expect: {"overall":"passed","facts":{"published":true}}

[Verify] primary-proof
run: src_compile
tests: ["PrimarySuite.*"]
expect: {"overall":"passed"}

[SetGoal]
run: src_compile
tests: ["PrimarySuite.*"]
expect: {"overall":"passed"}

[STEP] gear-up
[StepJob]
Choose the profile and run src_compile before any source edits.
[CheckList]
- Submit gear-up-published with the selected profile
- Record the published snapshot or typed publication failure
[Submit] gear-up-published
[Branch]
success: work
failure: gear-up

[STEP] work
[StepJob]
Do the smallest fact-informed implementation work for the request.
[CheckList]
- Submit primary-proof
- Attach relevant fact_refs to executor tasks
[Submit] primary-proof
[Branch]
success: END
failure: gear-up
```

After AddPlayBook succeeds, report the opening name, step graph, predicates, capabilities, and
the first command the user can run with `/arbiter-play`.
