# Arbiter Playbook Format

```markdown
---
name: hotfix-verify
description: 修复构建失败并验证回归的标准流程。适用于 CI 红灯、编译报错场景。
max_steps: 32
---

[SetGoal]
shell: make test

[STEP] diagnose
[StepJob]
定位构建失败的直接原因。阅读最近一次构建日志,确认失败的目标与首个报错,
给出修复方向。不要修改任何代码。
[CheckList]
- 产出失败根因结论与证据文件路径
- 确认失败可在本地复现
[Branch]
success: fix
failure: diagnose

[STEP] fix
[StepJob]
按上一步结论实施最小修复,只允许修改与根因直接相关的文件。
[CheckList]
- 完成修复且构建通过
- 现有测试全部通过
[Branch]
success: END
failure: diagnose
```

Naming & dedup (binding for every playbook in this directory):
- `name` is the USER INTENT as an imperative phrase: verb-first, kebab-case,
  at most 3 segments — `fix-reported-bug`, `hunt-latent-bugs`, `build-feature`,
  `fix-slow-path`. Not the method, not the mechanism, never a codename.
- The file name is `<name>.md` — they must match exactly.
- `description` starts with "Use when …" (the curator's first selection
  signal) and contains a "Do not use … (use <other-playbook>)" cross-pointer
  whenever another playbook's intent is adjacent — dedup happens at selection
  time, in the description, not by hoping nobody notices the overlap.
- One playbook per distinct intent. Before adding, read the existing names: if
  the intent overlaps an existing book, extend that book instead of forking a
  near-copy. `AddPlayBook` refuses duplicate names; near-synonym names are on
  you.

Predicate discipline (what makes a playbook worth the referee):
- Every step whose work is checkable tells the executor the EXACT result
  predicate to submit — a concrete shell command (mind the exit-code polarity)
  or an mcp call with `expect` clauses on structuredContent fields. A step
  that only says "verify it works" will be gamed by the first trivially-true
  predicate an executor invents.
- Encode laws as machine checks inside the predicate, not as prose: test-file
  untouchability is `git diff --quiet -- <paths> && …`, determinism is a 5x
  shell loop, measured improvement is two expect-clause measurements compared
  against a recorded noise band.
- Checklist items must be mechanically checkable — each one should map to a
  predicate or an artifact a reviewer can open. If you cannot write the check,
  the step is mis-split: redesign it.

Rules:
- File size must be at most 1 MiB.
- Frontmatter must include `name` and `description`; optional `max_steps`
  is the round budget (default 256, max 1024) — the match aborts with
  `steps_exhausted` once spent.
- Optional `[SetGoal]` (before the first `[STEP]`, at most once) declares the
  checkmate predicate: `shell: <command>` or `mcp: <server> <tool>` plus
  optional `arguments: {...json}`, `timeout_s`, `output_lines`. mcp goals may
  add `expect: [{"path":...,"op":"eq|ne|ge|le|exists","value":...}, ...]`
  (≤8 clauses, scalar values, dotted paths) — the clauses are compared against
  the tool's structuredContent typed fields and ALL must hold; without
  `expect` an mcp goal passes on any non-error response, so prefer `expect`
  whenever the server reports structured results. After any successful round
  adjudication the predicate runs; pass = checkmate = the match finishes
  successfully at once. Reaching `END` on the success branch while the goal
  still fails finishes the match as a failure.
- Each step needs `[StepJob]`, `[CheckList]`, and `[Branch]`.
- Branch keys are exactly `success` and `failure`; targets are a step name or `END`.
- A step may carry an optional `[Gotcha]` section (`- ` items, may be empty):
  reusable caveats for that step, returned alongside it on every ShowStepJob.
  Usually you do not write these by hand — the player model appends them at
  run time via NotePlaybook as it discovers pitfalls.
