---
name: playbook-create
description: Interview the user and register a new Arbiter opening.
---

Create a playbook by interviewing first, then drafting, then calling AddPlayBook.

Ask for missing information in one pass: intended request class, steps, failure branches,
machine-checkable predicates, primary recipe or fact goal, and any required capabilities.

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
- Checklists must be fact- or run-groundable; never ask a model to decide success.
- Gotchas are one-line, step-scoped, append-only notes.

Use this scaffold and adapt only the names, descriptions, branches, predicates, and task text:

```markdown
---
name: new-opening
description: One sentence describing the request class this opening handles.
max_steps: 32
---

[Verify] gear-up-published
run: src_compile
tests: ["src_compile"]
expect: {"overall":"passed","facts":{"published":true}}

[Verify] primary-proof
run: primary
tests: ["PrimarySuite.*"]
expect: {"overall":"passed"}

[SetGoal]
run: primary
tests: ["PrimarySuite.*"]
expect: {"overall":"passed"}

[STEP] gear-up
[StepJob]
Choose the profile and run src_compile before any source edits.
[CheckList]
- Submit gear-up-published with the selected profile
- Record the published snapshot or typed publication failure
[Branch]
success: work
failure: gear-up

[STEP] work
[StepJob]
Do the smallest fact-informed implementation work for the request.
[CheckList]
- Submit primary-proof
- Attach relevant fact_refs to executor tasks
[Branch]
success: END
failure: gear-up
```

After AddPlayBook succeeds, report the opening name, step graph, predicates, capabilities, and
the first command the user can run with `/arbiter-play`.
