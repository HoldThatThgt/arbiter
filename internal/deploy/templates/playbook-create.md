---
name: playbook-create
description: 根据用户描述创建新的流程棋谱。当用户要求"新建/起草/生成一个 playbook、流程、棋谱"时使用。
---

你要把用户的流程描述转写成一份合法棋谱并注册。

## 步骤

1. 缺什么问什么,一次问全:目标与适用场景;步骤划分;每一步的完成标准
   (必须可被 shell 命令或 mcp 调用客观验证);失败时去哪(原地重试/回退/放弃);
   整体成功判据(checkmate 条件,可选但推荐);步数预算 max_steps(可选,默认 256)。
2. 定名(强制规约,见 FORMAT.md"Naming & dedup"):name 是用户意图的祈使短语
   ——动词开头、kebab-case、≤3 段(如 fix-reported-bug / build-feature),不是
   方法名、不是机制名、不是代号。description 以 "Use when …" 开头;若与既有
   棋谱意图相邻,必须写 "Do not use …(use <某棋谱>)" 互指。先看一眼既有棋谱
   名:意图重叠就扩展那一份,不要造近义词分身。
3. 按下方格式起草全文,执行谓词纪律(FORMAT.md"Predicate discipline"):
   - 每个可检验的步骤,在 [StepJob] 里写出执行席要提交的【确切】result 谓词
     ——具体 shell 命令(注意退出码极性)或 mcp 调用 + expect 子句;
   - 规则写成机器检查而非散文:测试不可改 = `git diff --quiet -- <paths> && …`,
     确定性 = 5 次循环,性能改善 = 两次 expect 度量对照噪声带;
   - checklist 每条可机器验证,写不出验证就回去重拆步骤;
   - 有明确终态判据就写 [SetGoal]——goal 通过即 checkmate。
4. 调用 AddPlayBook(content=全文)注册。返回 playbook_invalid 时按 data.issues
   逐条修正后重交;name_conflict 时说明撞名并回到第 2 步重新定名,不要覆盖。
5. 注册成功后向用户复述:名称、步骤图(step → success/failure 去向)、各步谓词、
   goal 与 max_steps,并提示可立即试跑(/arbiter-play …)。

## 格式

```markdown
---
name: 短横线小写名
description: 一句话写清适用场景与不适用场景(挑选棋谱时的第一信号)
max_steps: 32        # 可选,回合预算,默认 256,上限 1024
---

[SetGoal]            # 可选,checkmate 谓词,通过即整局胜利
shell: make test     # 或 mcp: <server> <tool>(可附 arguments: {...JSON})
timeout_s: 900       # 可选;另有 output_lines 可选

[STEP] 步骤名
[StepJob]
这一步要模型完成什么(自然语言)。
[CheckList]
- 可验证的完成标准一
- 可验证的完成标准二
[Branch]
success: 下一步骤名或END
failure: 某步骤名或END
```

另有可选的步骤级 `[Gotcha]` 节(`- ` 项):该步的踩坑注记,随 ShowStepJob 返回。
通常不必起草——对局中由棋手经 NotePlaybook 自动沉淀;用户明确给出已知坑时才写。

## 铁律

- 不要读取或猜测 .arbiter/match/playbook 下的内容;注册新棋谱的唯一途径是 AddPlayBook。
- 不替用户发明业务规则:流程细节不确定就问,不要编。
- checklist 写不出对应的验证命令/接口时,说明该步骤划分有问题,回去重新拆。
