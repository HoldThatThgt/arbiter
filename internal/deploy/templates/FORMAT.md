# Arbiter Playbook Format

```markdown
---
name: hotfix-verify
description: 修复构建失败并验证回归的标准流程。适用于 CI 红灯、编译报错场景。
max_steps: 32
verify_policy: named
---

[Verify] suite-green
# Full-line comments (first non-space character '#') are allowed here.
run: unit
tests: ["*"]
expect: {"overall":"passed","max_failed":0}
allow_overrides: ["tests"]

[SetGoal]
verify: suite-green

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
  (Design-canonical intro openings — freeplay, gold-digger, recipe-derivation,
  regression-triage — are grandfathered by ADR-0012.)
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
  predicate to submit — a concrete shell command (mind the exit-code polarity),
  an mcp call with `expect` clauses on structuredContent fields, a typed
  run/fact spec, or a curated `[Verify]` name. A step that only says "verify
  it works" will be gamed by the first trivially-true predicate an executor
  invents.
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
- Optional frontmatter `capabilities` is a list whose only legal value is
  `recipes`; it capability-gates the recipe-authoring tools
  (`register` / `scan`) so they are exposed only while a
  playbook that opts in is the active match. Any unknown value, a
  non-`recipes` entry, or a duplicate is a parse error.
- Optional `[SetGoal]` (before the first `[STEP]`, at most once) declares the
  checkmate predicate: `shell: <command>`, `mcp: <server> <tool>` plus
  optional `arguments: {...json}`, `run: <recipe>` with `tests`/`expect`,
  or `fact: <query>` with `expect`; all kinds accept `timeout_s`,
  `output_lines`. After any successful round adjudication the predicate
  runs; pass = checkmate = the match finishes successfully at once.
  Reaching `END` on the success branch while the goal still fails finishes
  the match as a failure.
- Each step needs `[StepJob]`, `[CheckList]`, and `[Branch]`.
- Branch keys are exactly `success` and `failure`; targets are a step name or `END`.
- A step may carry an optional `[Gotcha]` section (`- ` items, may be empty):
  reusable caveats for that step, returned alongside it on every ShowStepJob.
  Usually you do not write these by hand — the player model appends them at
  run time via NotePlaybook as it discovers pitfalls.
- A step is either a TASK step (`[CheckList]`) or a CHECKPOINT step
  (`[Checkpoint]`) — exactly one, never both. A `[Checkpoint]` step has
  no executor predicate: it is a human-confirmation gate, adjudicated by the user's
  pass/fail on the question. Write a bare `[Checkpoint]` token and put the question
  on the following body line(s) — unlike `[STEP]`/`[Verify]`/`[Submit]`, an inline
  `[Checkpoint] <question>` on the same line is a `stray_content` parse error. The
  player must put the question to the user with
  AskUserQuestion and relay the answer via SubmitCheckpoint (pass → success branch,
  fail → failure branch); CreateTask is refused on it, and it cannot carry `[Submit]`.
  Use it for the one thing the referee cannot machine-check — a person's approval
  (e.g. "do these scenarios capture what you want?"). It is a human gate, not an
  anti-adversarial one: the user is in the loop and sees the question.
- A step may carry an optional `[Submit] <verify-name>` line: the curated
  `[Verify]` predicate this step's task MUST submit. The referee rejects any
  SubmitTask for this step whose result is not `{"verify": "<that-name>"}`
  (its `allow_overrides` fields may ride along) — an inline spec or a different
  curated predicate fails with `step_submit_mismatch`. This binds the proof to
  the step so the model can neither author the predicate nor pick a weaker
  curated one; its only freedom is the curator-whitelisted override (typically
  the test name). The name must match a `[Verify]` section. Strongly recommended
  on every checkable step: a step that says "Submit X" in prose but does not
  bind it with `[Submit] X` can be gamed by any trivially-true predicate.

Named `[Verify]` predicates:
- `[Verify] <name>` sections (any number, names are `[A-Za-z0-9_-]+` and
  unique) declare curated result predicates using the same `key: value`
  grammar as `[SetGoal]`. On LoadPlayBook they are snapshotted into match
  state — editing the playbook file mid-match cannot swap a predicate
  under an open round.
- The executor invokes one by name: SubmitTask with
  `{"result": {"verify": "<name>"}}`. The referee resolves the name
  against the match snapshot, then runs the usual validate → recipe-pin →
  execute pipeline. Unknown names fail with `verify_not_found`;
  ShowStepJob lists the available names with their kinds.
- A `verify` reference is mutually exclusive with every inline spec key —
  mixing them is rejected.
- Curated specs are closed by default. A `[Verify]` section may opt
  specific fields open with `allow_overrides: ["tests", "options"]`
  (only those two values are legal; expectation, kind, recipe, and command
  can never be overridden). A submission may then pass `tests`/`options`
  alongside `verify`; supplying an override the spec does not allow fails
  with `verify_override`. `allow_overrides` is curator-only: it is illegal
  in `[SetGoal]` and on submitted specs.
- Optional frontmatter `verify_policy: open | named` (default `open`).
  Under `named`, SubmitTask rejects inline specs with `verify_policy` —
  every task verdict must come from a curated predicate. `named` with zero
  `[Verify]` sections is a parse error.
- Goal aliasing: `[SetGoal]` may consist of the single line
  `verify: <name>`, resolving to a copy of that named predicate at parse
  time (section order does not matter). No other keys may accompany it,
  and a `[Verify]` section cannot itself use `verify:`.

Comment grammar (predicate sections):
- Inside `[SetGoal]` and `[Verify]` sections, a line whose first non-space
  character is `#` is a comment and is skipped.
- Inline `#` comments are **not** supported: values run to the end of the
  line. Fields whose grammar excludes `#` fail loudly at parse with a hint
  (JSON fields like `expect`/`arguments`/`tests`/`options`, integer fields
  like `timeout_s`/`output_lines`, `mcp`, `run` recipe ids — which must
  match `[A-Za-z0-9_-][A-Za-z0-9._-]*` without `..` — and `fact` query
  terms, which may not start with `#`).
- `shell:` values are taken verbatim to the end of the line. Note that
  `/bin/sh` itself treats an unquoted trailing `#` as a comment, so a
  trailing `# note` in a shell command is dropped by the shell, not by the
  parser.
