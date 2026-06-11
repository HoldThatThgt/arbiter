# Chess Playbook Format

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

Rules:
- File size must be at most 1 MiB.
- Frontmatter must include `name` and `description`; optional `max_steps`
  is the round budget (default 256, max 1024) — the match aborts with
  `steps_exhausted` once spent.
- Optional `[SetGoal]` (before the first `[STEP]`, at most once) declares the
  checkmate predicate: `shell: <command>` or `mcp: <server> <tool>` plus
  optional `arguments: {...json}`, `timeout_s`, `output_lines`. After any
  successful round adjudication the predicate runs; pass = checkmate = the
  match finishes successfully at once. Reaching `END` on the success branch
  while the goal still fails finishes the match as a failure.
- Each step needs `[StepJob]`, `[CheckList]`, and `[Branch]`.
- Branch keys are exactly `success` and `failure`; targets are a step name or `END`.
- A step may carry an optional `[Gotcha]` section (`- ` items, may be empty):
  reusable caveats for that step, returned alongside it on every ShowStepJob.
  Usually you do not write these by hand — the player model appends them at
  run time via NotePlaybook as it discovers pitfalls.
