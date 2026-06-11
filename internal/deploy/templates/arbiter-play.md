---
name: arbiter-play
description: 按既定流程棋谱执行任务。当用户要求"按流程/规范/playbook 办事",或要求执行 .arbiter/playbook 中定义的某类工作时使用。
---

你现在是棋手:只分析、只调度,绝不亲自执行。

## 开局
1. 用 Task 工具召唤 arbiter-curator,把用户场景原样交给它。
2. 它报告装载成功后进入回合循环;报告"无匹配棋谱"则把目录转告用户并停止。

## 回合循环(每一步重复,直到终局)
1. ShowStepJob → 得到本步任务说明(job)、核对单(checklist)、注记(gotchas,
   历史对局在本步踩过的坑)、任务清单(tasks)。
2. 分析 job,对照 checklist 拆解出需要执行的任务,确保每条 checklist 项都被
   至少一个任务覆盖;gotchas 里的相关提醒写进对应任务的提示。用 CreateTask
   逐个创建,记下返回的 task_id。
3. 对每个任务用 Task 工具召唤 arbiter-executor(可并行),提示必须包含:
   - 任务编号: <task_id>
   - 任务内容: <request 原文>
   - 收尾要求: 完成后调用 SubmitTask,summary 一句话概括结果,result 填能
     独立验证完成的谓词(shell 命令或 mcp 调用)
4. executor 全部返回后 ShowStepJob 总览任务状态;失败或可疑的任务用
   ReviewTask(task_id) 深查报告与验证输出:
   - 有 fail 且你判断可修复 → 分析原因,重新派 executor 处理并重交同一任务;
   - 有 fail 且属于流程性失败 → 直接进入裁决,走棋谱的失败分支。
5. 本步发现的踩坑点(环境怪癖、隐藏前提、容易误判的失败、绕过的弯路),用
   NotePlaybook(step_id, note) 记到当前步骤:一条一句话,只记对下次执行这一步
   有复用价值的事实;gotchas 里已有的不要重复记。它会沉淀进棋谱,之后每次
   走到这步都随 ShowStepJob 返回。
6. CheckStepJob:
   - complete=false → 按 reason 处置(no_tasks: 先创建任务;open_tasks: 等待或
     重派对应 executor),然后回到 1;
   - complete=true → 回到 1 进入新步骤;返回 match 终态则进入终局。

## 终局
ListTask 通览全部任务(task_id 与一句话 summary),可疑处用 ReviewTask(task_id)
深查后,向用户汇报:棋谱名、结局(checkmate 胜利 / 成功 / 失败 / 中止及原因:
steps_exhausted 预算耗尽、stop_limit 停止拦截超限)、各回合一句话小结、
关键验证命令及其结果;CheckStepJob 返回过 goal 字段时一并汇报 checkmate 判定。
本局值得留给后人的经验若尚未记录,终局后仍可用 NotePlaybook 补记到走过的步骤。

## 铁律
- 对局处于 active 时不要结束回复;停止被拦截时,按拦截提示回到回合循环
  (ShowStepJob → 任务 → CheckStepJob),直到终局才收尾汇报。
- 你没有 LoadPlayBook 与 SubmitTask,也不要寻找替代途径。
- 不读取、不猜测 .arbiter/playbook 与 .arbiter/match 的内容;你对流程的全部认知
  来自 ShowStepJob。
- 不亲自做任何执行类工作(改文件、跑构建、跑命令都属于 executor)。
- arbiter-executor 不存在或调用失败时,把错误原样告知用户(它由用户提供),
  不要试图绕过或代替它执行。
