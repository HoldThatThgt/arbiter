# 棋谱格式规格

棋谱是 `.chess/playbook/` 下的 `.md` 文件:YAML frontmatter + 若干 STEP 区块。
装载时一次性解析校验,违例返回 `playbook_invalid` 与逐条结构化 issue。

## 完整示例

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

## 文法(行式)

| 行类型 | 形式 | 语义 |
|---|---|---|
| 将死谓词 | `[SetGoal]` | 可选,至多一处,必须在首个 `[STEP]` 之前;节内为 `key: value` 行,键集 `{shell, mcp, arguments, timeout_s, output_lines}`,shell/mcp 二选一(`mcp: <server> <tool>`,arguments 为 JSON 对象一行)。**每次成功裁决后执行,通过即 checkmate,整局立即胜利**;到达 END 而 goal 未过则整局失败 |
| 步骤头 | `[STEP] <name>` | 开启新步骤;name 为行剩余部分去首尾空白,非空、全谱唯一;首个 `[STEP]` 是入口步骤 |
| 节头 | `[StepJob]` / `[CheckList]` / `[Branch]` / `[Gotcha]` | 行内仅含该标记;开启对应小节 |
| StepJob 内容 | 任意行 | 原样累积为自然语言 prompt(含空行) |
| CheckList 项 | 首 token 为 `-` 的行 | 剩余部分为一条 checklist 文本 |
| Gotcha 项 | 首 token 为 `-` 的行 | 剩余部分为一条注记文本。`[Gotcha]` 节可选、可空,不参与三节齐全校验;随 ShowStepJob/ReadPlayBook 返回。通常由棋手在对局中经 NotePlaybook 追加(单行 ≤ 1024 字节),手写已知坑亦可 |
| Branch 项 | `success: <target>` / `failure: <target>` | 按首个 `:` 切分;target 为某步骤名或保留字 `END` |
| 空行 | — | StepJob 节内保留;其余位置作为分隔跳过 |
| 其他非空行 | — | 节外或 CheckList/Branch 节内出现即报 `stray_content` |

frontmatter 必含 `name` 与 `description`(curator 选谱依据之一),可选 `max_steps`
(回合预算,默认 256、上限 1024,耗尽即 `aborted/steps_exhausted` 强制终局);
`name` 与文件名无关,目录内唯一。解析是结构化分词(标记整词相等比较),不存在
正则与模糊匹配——`[STEP]name`(缺空格)不是步骤头,会按内容行处理并报错。

## 校验错误码

| code | 条件 |
|---|---|
| `bad_frontmatter` | frontmatter 缺失/非法,或缺 name/description |
| `no_steps` | 无任何 `[STEP]` |
| `duplicate_step` | 步骤名重复 |
| `missing_section` | 某步骤缺三节之一([Gotcha] 是可选节,不计) |
| `empty_job` | StepJob 为空 |
| `empty_checklist` | CheckList 无任何项 |
| `bad_branch` | Branch 缺 success/failure、键重复或未知键 |
| `unknown_branch_target` | target 既非现存步骤名也非 END |
| `stray_content` | 不合文法的内容行 |
| `bad_goal` | [SetGoal] 重复/位置错误/键非法/缺谓词/arguments 非 JSON/数值越界 |
| `bad_max_steps` | max_steps 超出 1..1024 |
| `oversize` | 文件超过 1 MiB |
| `name_conflict` | 目录内多文件同名(冲突各方均不可装载) |

## 编写建议

- **checklist 写成可验证的谓词**:每条 checklist 项都应该存在一条 shell 命令或 MCP
  调用能客观判定它(executor 提交的 result 就是这条谓词)。"测试全部通过"好;
  "代码质量良好"无法机器裁决,只能靠棋手的分析判断兜底。
- **failure 分支即重试策略**:`failure: 自身` 表示原地重试;`failure: 上一步` 表示回退
  重诊;`failure: END` 表示体面放弃(对局以 finished_failure 终局)。循环的唯一边界
  是回合预算 `max_steps`(默认 256),耗尽即 `aborted/steps_exhausted`——给重试型
  棋谱设个贴合的预算。
- **有客观终态就写 [SetGoal]**:goal 是比逐步 checklist 更高一级的判据——通过即
  checkmate,模型既不能提前宣布胜利,也不能在将死前停手(停止门控会把它拦回来,
  直到 checkmate 或预算耗尽)。goal 与"loop 到自身的单步棋谱"组合,就是
  "反复尝试直到通过"的标准写法。
- **description 写适用条件**而不只是名字的同义反复:curator 通读全文选谱,但 description
  是第一道筛选信号(任务类型、适用场景、不适用场景)。
- **[Gotcha] 留给对局去写**:它是棋谱的经验层——棋手在执行中发现的坑经 NotePlaybook
  逐条沉淀(只增不改,同名注记幂等),下次走到该步随 ShowStepJob 自动返回。起草时
  通常留空;用户明确给出已知坑时才预写。注记是给未来棋手看的提示文本,定期人工
  复查、把过时或啰嗦的条目清掉(直接编辑文件即可)。
- 步骤名用短 slug(`diagnose`/`fix`),Branch 引用时不易写错。
- 单文件 ≤ 1 MiB;目录总量保持适度——curator 通读模式会返回全部棋谱全文。
