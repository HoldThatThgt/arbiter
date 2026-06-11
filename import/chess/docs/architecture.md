# 总体架构

Chess 在 Claude Code(下称宿主)之上实现"按棋谱办事"的流程控制。三个角色、一个二进制:

| 角色 | 实体 | 职责 | 可调用接口 |
|---|---|---|---|
| 棋手 player | 宿主主对话 | 分析 StepJob、拆解任务、调度 executor、请求裁决、沉淀 gotcha、起草新棋谱 | ShowStepJob / CreateTask / CheckStepJob / ListTask / ReviewTask / NotePlaybook / AddPlayBook |
| 领谱 curator | 自带 subagent `chess-curator` | 通读全部棋谱,按场景挑选并装载 | ReadPlayBook / LoadPlayBook / ListTask / ReviewTask |
| 执行 executor | 用户提供 subagent `chess-executor` | 完成单个任务并提交一句话 summary 与验证谓词 | SubmitTask / ListTask / ReviewTask |
| 裁判 arbiter | `chess serve <seat>` 本地进程 | 棋谱解析、对局状态机、谓词核验、裁决、日志 | —(确定性代码,无模型参与) |

```text
                       Claude Code session (一个仓库)
   ┌──────────────────────────────────────────────────────────────────┐
   │  棋手(主对话, skill: chessplay 提供操作规程)                       │
   │   ├─ Task → chess-curator ── ReadPlayBook + LoadPlayBook ──┐      │
   │   ├─ ShowStepJob / CreateTask / CheckStepJob / ListTask /  │      │
   │   │  ReviewTask / NotePlaybook / AddPlayBook               │      │
   │   │         │                                              ▼      │
   │   │   [chess serve player]                  [chess serve curator]*│
   │   │    ↑ .mcp.json 注册,session 常驻                              │
   │   └─ Task → chess-executor ── SubmitTask ──► [chess serve executor]*
   └──────────────────────────────────────────────────────────────────┘
        * 内联 mcpServer,随 subagent 启停,进程存活仅数秒~数分钟

   席位进程共享同一份磁盘状态(flock 串行化):
       .chess/run/state.json     对局真相(含棋谱快照;对宿主文件工具封禁)
       .chess/status.json        可观察投影(只含当前与历史,安全)
       .chess/log/journal.jsonl  全量行为日志
       .chess/playbook/*.md      用户棋谱(对宿主文件工具封禁)
```

## 席位隔离(核心机制)

需求:棋手不得调用 LoadPlayBook 与 SubmitTask,不得访问棋谱目录。两类直觉方案在宿主
机制下不成立(deny 规则对 subagent 同样生效;hook 输入无调用方身份字段,详见
[decisions.md](decisions.md) 附录),最终采用**按席位拆分服务器**的构造性方案:

| 席位进程 | 注册位置 | 暴露工具 |
|---|---|---|
| `chess serve player` | 仓库 `.mcp.json`(全 session 可见) | ShowStepJob, CreateTask, CheckStepJob, ListTask, ReviewTask, NotePlaybook, AddPlayBook |
| `chess serve curator` | curator agent frontmatter 内联 `mcpServers` | ReadPlayBook, LoadPlayBook, ListTask, ReviewTask |
| `chess serve executor` | executor agent frontmatter 内联 `mcpServers` | SubmitTask, ListTask, ReviewTask |

宿主保证内联 mcpServers 只在对应 subagent 运行期间挂载、主对话不可见。于是受限接口在
主对话的工具清单里**根本不存在**——不是被拦截,而是无路由,fail-closed 由构造保证。
在此之上还有两层:特权席位的**启动凭证**(冒名拉起的进程拒绝服务)与路径 deny 规则,
完整论述见 [security.md](security.md)。

## 进程与状态模型

- 席位进程**无内存状态**:每次工具调用都是 `flock → 读 state.json → 变更 → 临时文件+rename
  原子写(state 与 status 同步)→ 解锁`。进程随起随停不丢状态,天然支持多进程并发
  (并行 executor 即多个 executor 席位进程)。
- 状态放文件而非内存,是因为 curator/executor 进程与 player 进程生命周期不同、内存不共享,
  而引入常驻共享进程违背小型化;小 JSON + flock 在毫秒级,顺带免费获得用户可观察性。
- **单仓库单活动对局**:LoadPlayBook 装载新棋谱即替换旧对局(journal 记 `match_replaced`)。
- **棋谱快照语义**:装载时一次性解析为结构化快照写入 state,对局期间不再读棋谱源文件
  ——对局确定性,中途编辑棋谱不影响进行中的对局。state 含未来步骤故放封禁目录;
  对外的 status.json 只投影当前与历史。唯一例外是 gotcha 注记:NotePlaybook 把单行
  注记同时追加进源文件与快照的 `[Gotcha]` 节(只增不改,写盘前整体重解析复核)——
  流程本体(job/checklist/branch/goal)的快照不可变性不受影响。

## 一局的生命周期

```text
用户请求 → 棋手召唤 curator:通读全部棋谱 → 选谱 → 装载(回合 1 = 入口步骤)
回合循环:
  ShowStepJob   棋手取局面:job + checklist + gotchas(该步沉淀的注记)+ 任务清单
  CreateTask×N  按 checklist 拆解任务(每条至少一个任务覆盖,gotchas 写进任务提示)
  Task→executor 并行派发,提示携带 task_id + request
     executor 干活 → SubmitTask(task_id, 一句话 summary, report, 验证谓词)
     裁判锁外执行谓词:exit 0 / isError=false → pass,否则 fail(可重交,最后一次为准)
  ReviewTask    按需深查失败任务
  NotePlaybook  本步发现的坑记成单行注记:写进棋谱文件与对局快照,沉淀给未来对局
  CheckStepJob  请求裁决(确定性):
     无任务/有未交任务 → 未完成,棋手继续
     全部通过   → success 分支;有失败 → failure 分支
     棋谱声明 [SetGoal] 时,成功裁决后锁外执行 checkmate 谓词:
        通过 → 无视分支立即 finished_success(checkmate)
        未过且分支为 END → finished_failure(goal 是唯一胜利判据)
     分支为 END(无 goal)→ finished_success / finished_failure
     回合预算耗尽(max_steps,默认 256)→ aborted/steps_exhausted
终局 → 棋手 ListTask 通览全部任务的 summary,向用户汇报
       (棋谱名、结局、各回合小结、关键验证及结果)
```

**停止门控**:init 在宿主注册 Stop hook(`chess hook stop`)。对局 active 时模型每次
试图结束回复都会被结构化拦截,拦截理由携带局面(回合 i/预算、当前步骤、任务计数)
与"继续规程"的指引——**步数未耗尽且没有 checkmate 之前模型停不下来**;终局
(checkmate / finished / steps_exhausted / stop_limit)后自然放行。同一回合内拦截
超过 32 次(无进展的卡死保护)→ `aborted/stop_limit` 并放行;门控故障 fail-open;
用户的人工中断不经 hook,永远可用。

裁决权在裁判、时机在棋手:棋手决定**何时**请求裁决,裁判按结构化规则裁决**结果**。
模型无法谎报成功——成败唯一来源是验证谓词的结构化结局,自然语言永不参与判定。

## 结构化边界(无正则约定的落实)

- **解析层**(一次性反序列化):棋谱文本经行式分词器转为结构化模型——行首 token 与
  封闭标记集整词相等比较、Branch 行按首个 `:` 切分,无正则、无子串扫描;frontmatter
  走 YAML 库。
- **运行层**(全部决策):分支跳转、完成性、成败、席位能力,只消费枚举/计数/exit code
  /isError。checklist 不做机器核对(语义核对必然要文本匹配),覆盖性由棋手分析负责、
  chessplay 规程强制,机器只裁决"已建任务是否全部验证通过"。

## 性能与体积

单一静态二进制(Go,仅两个直接依赖);席位进程毫秒级启动(curator/executor 随用随起);
状态文件 KB 级;无守护进程、无网络、无数据库。非目标:Windows、持久化格式兼容、
多对局并发、远程协作。

模块划分与各自设计见 [modules/](modules/) 目录。
