# 模块设计:internal/match

对局状态机:状态文件的唯一所有者,十个接口的领域逻辑都在这里落地。

## 职责与边界

- 持有 `Match/Round/Task` 模型与全部状态迁移(装载/建任务/提交/裁决/检索/注记)。
- 状态文件 IO:flock 串行化、临时文件 + rename 原子写、status.json 投影同步。
- **不做**:谓词执行细节(委托 verify)、MCP 协议(seat 负责)、文件路径之外的宿主概念。

## 核心类型

```go
Store { Root, Seat }                      // 无内存状态,仅路径与席位名(用于日志)
Match { ID, Playbook(快照), Status, Abort, Current *Round, History []Round,
        TaskSeq, RoundSeq, StartedAt }
Round { Seq, StepID, Tasks []Task, Outcome, EnteredAt }   // 步骤的一次进入
Task  { ID("T"+seq 全局递增), Request, Status(open|pass|fail),
        Summary(executor 提交的一句话概要), Report, Result }
ToolError { Code, Message, Data }         // 接口错误的统一载体
```

状态机:`active → finished_success | finished_failure | aborted(replaced|steps_exhausted|stop_limit)`;
state.json 不存在即 idle。同一步骤被分支重入产生**新 Round**(任务清零重计),
已裁决 Round 连同任务终态整体归档进 History。

## 并发协议(store.go)

```text
withLock(fn):
  flock(.chess/run/lock, LOCK_EX|LOCK_NB, 25ms 重试, 5s 超时 → state_busy)
  → 读 state.json(不存在 → nil;损坏 → state_corrupt)
  → fn 变更 → 原子写 state.json + 同步投影 status.json → 解锁
```

- 每次 `lock()` 独立打开 fd,因此同进程多 goroutine 与多 OS 进程走同一套互斥。
- 原子写:`CreateTemp + Write + Chmod + Sync + Rename`,任何失败都清理临时文件。
- 投影(status.go)只含 match 元信息、当前步(job/checklist/任务摘要)与历史回合摘要
  ——**绝不含未来步骤**,这是"棋手可见信息 = ShowStepJob ∪ 历史"不变式的实现点。

## SubmitTask 两段式(本模块最关键的协议)

谓词可能跑数分钟,绝不能占着状态锁:

```text
段0(锁外): summary 校验(非空、≤1024 字节 → bad_summary)
段1(锁内): 任务存在且属当前回合、谓词合法(verify.Validate)→ 记下 round_seq → 解锁
执行(锁外): verify.Execute(谓词)
段2(锁内): 若 round_seq 已变(执行期间棋局被裁决推进)→ task_stale,执行作废
           否则写 summary/report/result/status(exit 0 或 isError=false → pass)→ 原子写
```

迟到的验证结果永远不会污染新回合;重交允许(open/pass/fail 皆可),最后一次为准。

## 裁决算法(CheckStepJob,确定性,goal 路径两段式)

```text
evaluateRound(纯计算):
  任务集为空            → {complete:false, reason:no_tasks}
  存在 open 任务        → {complete:false, reason:open_tasks, open:[…]}
  全部 pass / 存在 fail → outcome 与 branch target

无 goal 或 outcome=failure → 单锁内 settle:
  归档 Round → END 则 finished_<outcome>;预算耗尽(RoundSeq+1 > StepBudget,
  默认 256)→ aborted/steps_exhausted;否则进入新 Round(StopBlocks 清零)

有 goal 且 outcome=success → 两段式(同 SubmitTask 哲学,goal 可能跑分钟级):
  段1(锁内) 仅计算与记 RoundSeq,不落子 → 解锁
  执行(锁外) verify.Execute(goal)
  段2(锁内) RoundSeq 变化 → {complete:false, reason:state_changed}
             重算 evaluateRound(期间可能有重交)→ 按重算结果 settle:
               goal 通过  → checkmate:finished_success(无视分支)
               goal 未过+END → finished_failure(goal 是唯一胜利判据)
               goal 未过+步骤 → 正常推进,输出附 GoalReport
```

裁决只数计数与枚举,不读任何自然语言;goal 结果同样只看 exit code / isError。

## 停止门控(StopGate)与新棋谱注册(AddPlayBook)

- `StopGate`(由 `chess hook stop` 调用):idle/终局 → 放行;active → `StopBlocks++`
  并以局面构造拦截理由;同回合超过 32 次 → `aborted/stop_limit` 放行(卡死保护,
  进入新回合时 settle 清零计数)。
- `AddPlayBook(content)`:全量解析校验 → 文件名安全校验(`filepath.Base` 等值)→
  锁内查同名(目录与文件双重)→ 原子写 `<name>.md`,只创建绝不覆盖。

## 装载与替换(LoadPlayBook / ReadPlayBook)

- 目录扫描与解析在**锁外**(纯读),状态创建在锁内。
- 装载即快照:`Match.Playbook` 内嵌完整解析结果,对局期间不再读棋谱源文件
  (唯一例外是 NotePlaybook 的注记追加,见下)。
- 已有活动对局 → journal 记 `match_replaced`(旧对局让位,日志即归档)。
- ReadPlayBook 纯读不碰状态;空目录返回空数组而非 null。

## 任务检索(ListTask / ReviewTask)

两者全席位、只读、终局后仍可用:ListTask 返回全部任务的索引行(编号/回合/步骤/
状态/summary,历史在前当前在后),ReviewTask 按 task_id 给全量细节——索引与细节
分离,通览不随 report/验证输出膨胀。

## 注记沉淀(NotePlaybook)

把对局中发现的 gotcha 追加进棋谱该步骤的 `[Gotcha]` 节,**双写**:源文件(沉淀给
未来对局)+ 当前快照(本局再到该步即随 ShowStepJob 返回)。全程锁内:

```text
锁外: note 校验(非空、单行、≤1024 字节 → bad_note)
锁内: 步骤须本局走过(当前或历史回合 → 否则 step_not_found,
      棋手可见信息不超出 ShowStepJob ∪ 历史)
      → ScanDir/Find 按 frontmatter name 定位源文件(文件名与棋谱名无关)
      → 重解析源文件,步骤存在、注记去重(已有 → added:false 幂等返回)
      → playbook.AppendGotcha 文本手术 → 体积查界 → 整体重解析复核
        (复核不过即拒写,绝不留下坏棋谱)→ 原子写文件
      → 快照 Steps[step].Gotchas 追加 → 原子写 state
```

终局后仍可补记(复盘窗口,步骤经历史回合可见)。

## journal 事件(经 store.append 在锁内顺带写出)

`match_started/replaced/finished/aborted`、`round_entered/adjudicated`、
`task_created/submitted`、`goal_checked`、`stop_blocked`、`playbook_added`、
`playbook_noted`。字段表见 [journal.md](journal.md)。

## 测试要点

裁决表驱动(no_tasks/open/全 pass/含 fail/END/上限/重入清零);替换装载;
ReviewTask 当前/历史/终局后检索;ListTask 索引序与 summary;summary 校验;
NotePlaybook 全路径(建节/追加/去重/未走过步骤拒绝/终局后补记/重装载可见);
并发:goroutine 并行提交 + 同时裁决(-race),慢提交被快提交+裁决超越 → task_stale。
