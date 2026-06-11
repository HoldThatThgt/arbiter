# MCP 接口规格

十个接口,单接口单语义;无会话参数、无令牌参数、无分页参数。

通则:

- 输入输出均为结构化 JSON;错误统一为 `{code, message, data?}`。
- **isError 仅用于调用方错误**(参数非法、对象不存在、状态不允许);领域状态
  (idle、未完成、已终局)是**正常结构化结果**,降低模型重试噪音。
- 所有调用(含错误)写入 journal 的 `tool_called` 事件。
- 席位归属即权限:接口只注册在对应席位的服务器上(见 [architecture.md](architecture.md))。

## ReadPlayBook(curator)

| | |
|---|---|
| 输入 | `{}` |
| 行为 | 解析 `.chess/playbook/*.md` 全部文件,只读返回每份棋谱的完整结构化内容,不改状态。棋谱目录对宿主文件工具封禁,这是 curator 通读棋谱的唯一通道 |

```json
{ "playbooks": [
    { "name": "hotfix-verify", "description": "…", "entry": "diagnose",
      "steps": [ { "id": "diagnose", "job": "…", "checklist": ["…"],
                   "branch": {"success": "fix", "failure": "diagnose"} } ] } ],
  "invalid": [ {"file": "broken.md", "line": 1, "code": "bad_frontmatter"} ] }
```

空目录返回空数组(非 null)。解析失败的文件进 `invalid`,不阻塞其余棋谱。

## LoadPlayBook(curator)

| | |
|---|---|
| 输入 | `{ "name": string }`(必填) |
| 行为 | 全量解析校验该棋谱 → 生成快照 → 替换可能存在的活动对局(journal 记 `match_replaced`)→ 创建新对局,入口步骤为回合 1 |

```json
{ "match_id": "m-20260610T060417Z-b529", "playbook": "hotfix-verify",
  "first_step": "diagnose", "steps_total": 2, "replaced_match": null }
```

错误:`playbook_not_found`(data.available 附可装载名称清单)、
`playbook_invalid`(data.issues 逐条)、`name_conflict`。

## ShowStepJob(player)

| | |
|---|---|
| 输入 | `{}` |
| 行为 | 返回当前局面总览:步骤 prompt、checklist、注记 gotchas(该步骤沉淀的踩坑提示,见 NotePlaybook)、本回合任务清单(编号/状态/request/已提交任务的 summary)。报告与验证细节走 ReviewTask,局面应答不随验证输出膨胀 |

```json
{ "status": "active", "playbook": "hotfix-verify", "round": 2,
  "step": { "id": "fix", "job": "…", "checklist": ["…", "…"],
            "gotchas": ["先 make clean,否则增量缓存掩盖失败"] },
  "tasks": [ {"id": "T3", "status": "pass", "request": "…", "summary": "…"},
             {"id": "T4", "status": "open", "request": "…"} ] }
```

其他状态(正常结果,非错误):
`{"status":"idle","hint":"无活动对局,请先通过 chess-curator 装载棋谱"}` /
`{"status":"finished_success","playbook":"…","rounds":4}` /
`{"status":"aborted","abort":"steps_exhausted",…}`。

## CreateTask(player)

| | |
|---|---|
| 输入 | `{ "request": string }`(非空) |
| 行为 | 在当前回合追加任务 `T<n>`(全局递增),初始状态 open。一次一个,需要多个就多次调用 |
| 返回 | `{ "task_id": "T4", "step_id": "fix" }` |
| 错误 | `no_active_match`、`empty_request` |

## SubmitTask(executor)

| | |
|---|---|
| 输入 | `{ "task_id": string, "summary": string, "report": string, "result": ResultSpec }` |
| 行为 | 校验 summary(一句话结果概要,非空、≤ 1024 字节,进任务清单供 ListTask/复盘检索)与任务属于当前回合且谓词合法 → **锁外**执行谓词 → 回锁复核回合未推进 → 写入 summary/report 与执行结局。shell: exit 0 → pass;mcp: isError=false → pass;其余皆 fail。任务可重交,最后一次为准 |
| 返回 | `{ "task_id": "T4", "verdict": "pass", "exit_code": 0, "output": "…尾部…", "duration_ms": 8123 }`(mcp 形态以 `is_error` 替代 `exit_code`;运行性失败附 `failure`) |
| 错误 | `no_active_match`、`task_not_found`、`task_stale`(任务所属回合已被裁决——执行期间棋局推进)、`bad_summary`、`bad_result`、`server_not_found`、`unsupported_transport`、`reserved_server` |

超时/无法启动/传输失败**不算调用方错误**:正常返回 `verdict:"fail"`,
`result.failure` 标 `timeout` / `spawn_error` / `transport_error`,留给棋手分析处置。

### ResultSpec(验证谓词,kind 判别的结构化二选一)

```json
{ "kind": "shell", "command": "make test" }
{ "kind": "mcp", "server": "github", "tool": "pr_checks", "arguments": {"pr": 42} }
```

| 字段 | 说明 |
|---|---|
| `kind` | `"shell"` 或 `"mcp"`(必填) |
| `command` | shell 形态:经 `/bin/sh -c` 执行,工作目录为仓库根 |
| `server` / `tool` / `arguments` | mcp 形态:服务器名取自仓库根 `.mcp.json`,仅支持 stdio;一次性拉起→调用→退出 |
| `timeout_s` | 可选,默认 600,上限 3600;超时杀整个进程组 |
| `output_lines` | 可选,默认 256,上限 10000;保留输出尾部 N 行 |

mcp 形态前置校验:`server_not_found`(名称不在 .mcp.json)、`unsupported_transport`
(非 stdio)、`reserved_server`(目标 command 解析后与 Chess 自身二进制相同——
防验证流程递归调用/改写对局状态;按路径相等判定,非名称比较)。

## CheckStepJob(player)

| | |
|---|---|
| 输入 | `{}` |
| 行为 | 对当前回合做确定性裁决:无任务或有未交任务 → 未完成;全部 pass → success 分支;存在 fail → failure 分支;分支为 END → 终局;回合预算耗尽 → 中止。棋谱声明了 `[SetGoal]` 时,**每次成功裁决后在锁外执行 checkmate 谓词**:通过 → 无视分支立即 `finished_success`(`checkmate: true`);未过且分支为 END → `finished_failure`(goal 是唯一胜利判据);未过且分支为步骤 → 正常推进并附带 goal 报告 |

```json
{ "complete": false, "reason": "open_tasks", "open_tasks": ["T4"] }
{ "complete": false, "reason": "no_tasks" }
{ "complete": true, "outcome": "success", "next_step": "fix", "round": 2,
  "goal": {"verdict": "fail", "exit_code": 1, "output": "…", "duration_ms": 90} }
{ "complete": true, "outcome": "success", "match": "finished_success",
  "checkmate": true, "goal": {"verdict": "pass", "exit_code": 0, "output": "", "duration_ms": 2} }
{ "complete": true, "outcome": "failure", "match": "finished_failure" }
{ "complete": true, "outcome": "success", "match": "aborted", "abort": "steps_exhausted" }
{ "complete": false, "reason": "state_changed" }
```

`state_changed`:goal 执行期间对局被替换/推进(罕见),重新调用即可;goal 执行期间
有任务重交的,裁决按重算后的任务终态进行。错误:`no_active_match`。
裁决推进后,ShowStepJob 即返回新步骤的 StepJob。

## AddPlayBook(player)

| | |
|---|---|
| 输入 | `{ "content": string }`(棋谱全文,frontmatter + 步骤) |
| 行为 | 全量解析校验 → **只创建,绝不覆盖**(名称取自 frontmatter,落盘 `<name>.md`)→ journal 记 `playbook_added`。不装载、不读取既有棋谱;配合 playbook-create skill 使用 |
| 返回 | `{ "name": "…", "file": "….md", "steps_total": 2, "max_steps": 256, "has_goal": true }` |
| 错误 | `playbook_invalid`(data.issues 逐条,含非法文件名)、`name_conflict`(同名已存在;不附目录清单,避免向棋手泄露) |

## ListTask(全席位)

| | |
|---|---|
| 输入 | `{}` |
| 行为 | 只读返回全部任务(历史回合在前、当前回合在后)的索引行:编号/回合/步骤/状态/summary。未提交的任务无 summary;细节走 ReviewTask。对局终局后仍可调用(复盘) |

```json
{ "tasks": [
    { "task_id": "T1", "round": 1, "step_id": "diagnose", "status": "pass",
      "summary": "定位到 Makefile 缺 vendor 目标" },
    { "task_id": "T2", "round": 2, "step_id": "fix", "status": "open" } ] }
```

无任务返回空数组(非 null)。错误:`no_match_loaded`(从未装载对局)。

## ReviewTask(全席位)

| | |
|---|---|
| 输入 | `{ "task_id": string }` |
| 行为 | 只读返回该任务全量信息(request/summary/report/status/含输出尾部的 result),在当前与历史回合中检索;对局终局后仍可调用(复盘) |

```json
{ "task_id": "T3", "round": 2, "step_id": "fix", "archived": false,
  "status": "pass", "request": "…", "summary": "…", "report": "…",
  "result": { "spec": {"kind": "shell", "command": "make test",
                        "timeout_s": 600, "output_lines": 256},
              "exit_code": 0, "output": "…尾部…", "duration_ms": 8123 } }
```

错误:`no_match_loaded`(从未装载对局)、`task_not_found`。

## NotePlaybook(player)

| | |
|---|---|
| 输入 | `{ "step_id": string, "note": string }`(note 单行、非空、≤ 1024 字节) |
| 行为 | 把对局中发现的 gotcha 以 `- ` 注记追加到棋谱该步骤的 `[Gotcha]` 节:**同时写棋谱源文件**(文本手术,既有内容原样保留;写盘前整体重解析复核,绝不留下坏棋谱)**与当前对局快照**(本局再到该步即随 ShowStepJob 返回)。仅限本局已走过的步骤(当前或历史回合)——棋手可见信息不超出 ShowStepJob ∪ 历史;同步骤相同注记幂等跳过(`added: false`)。终局后仍可补记。journal 记 `playbook_noted` |
| 返回 | `{ "playbook": "hotfix-verify", "step_id": "fix", "added": true, "gotchas": ["…", "…"] }` |
| 错误 | `no_match_loaded`、`bad_note`(空/多行/超长/会撑爆棋谱体积上限)、`step_not_found`(本局未走过该步骤,或棋谱文件已被人为改得没有它)、`playbook_not_found`(棋谱文件已被删除)、`name_conflict`、`playbook_invalid`(棋谱文件已被人为改坏,data.issues 逐条) |

## 错误码总表

| code | 接口 | 含义 |
|---|---|---|
| `playbook_not_found` | LoadPlayBook / NotePlaybook | 名称不存在(data.available 附清单)/ 棋谱文件已不在 |
| `playbook_invalid` | LoadPlayBook / AddPlayBook / NotePlaybook | 解析/校验失败(data.issues: [{file,line,code,detail}]) |
| `name_conflict` | LoadPlayBook / AddPlayBook / NotePlaybook | 同名冲突(AddPlayBook 拒绝覆盖) |
| `no_active_match` | CreateTask / SubmitTask / CheckStepJob | 无活动对局 |
| `no_match_loaded` | ListTask / ReviewTask / NotePlaybook | 从未装载过对局 |
| `empty_request` | CreateTask | request 为空 |
| `bad_summary` | SubmitTask | summary 为空或超过 1024 字节 |
| `bad_result` | SubmitTask | 谓词非法(kind 未知、形态字段残缺、timeout_s/output_lines 越界) |
| `server_not_found` | SubmitTask | mcp 谓词引用的服务器不在 `.mcp.json` |
| `unsupported_transport` | SubmitTask | mcp 谓词引用非 stdio 服务器 |
| `reserved_server` | SubmitTask | mcp 谓词引用 Chess 自身席位服务器 |
| `task_not_found` | SubmitTask / ReviewTask | 编号不存在 |
| `task_stale` | SubmitTask | 任务所属回合已被裁决 |
| `step_not_found` | NotePlaybook | 步骤本局未走过,或棋谱文件中已不存在 |
| `bad_note` | NotePlaybook | note 为空/多行/超过 1024 字节/会撑爆棋谱体积上限 |
| `state_busy` | 全部 | 状态锁获取超时(5s,可重试) |
| `state_corrupt` | 全部 | state.json 不可解析(重新装载即恢复) |

## 常量总表

| 常量 | 值 |
|---|---|
| 验证谓词超时 timeout_s | 默认 600 s,上限 3600 s(谓词字段可配) |
| 验证输出截断 output_lines | 默认尾部 256 行,上限 10000 行(谓词字段可配) |
| 单次验证输出硬上限 | 1 MiB(不可配,保护状态与日志) |
| 状态锁获取超时 | 5 s(25 ms 间隔重试) |
| 回合预算 max_steps(棋谱 frontmatter 可配) | 默认 256,上限 1024;耗尽 → `aborted/steps_exhausted` |
| 停止拦截上限(每回合,进入新回合清零) | 32 次;超限 → `aborted/stop_limit` 并放行停止 |
| 任务 summary 上限 | 1024 字节(非空,SubmitTask 必填) |
| gotcha 注记上限 | 单行 1024 字节(NotePlaybook) |
| 棋谱文件上限 | 1 MiB |
| 席位凭证 | `.chess/run/seat.key`,16 字节随机(32 hex 字符),env `CHESS_SEAT_KEY`,权限 0600 |
