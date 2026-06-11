# 模块设计:internal/seat

席位服务装配:把 match 的领域逻辑按席位挂到 MCP 协议上。**席位即权限**的实现点。

## 职责与边界

- `Run(ctx, root, seat)`:席位名校验 → 特权席位凭证校验 → seat_started/stopped 日志
  → 装配服务器 → stdio 运行至 EOF。
- 每席位一张工具表;handler = 参数解码 → match 调用 → 结构化应答;统一的
  `tool_called` 日志外环。
- **不做**:任何领域判断(全部委托 match/verify);**handler 内不存在任何身份判断**
  ——权限完全由"这个进程注册了哪些工具"表达。

## 工具表(buildServer)

| 席位 | 注册工具 |
|---|---|
| player | ShowStepJob, CreateTask, CheckStepJob, ListTask, ReviewTask, NotePlaybook, AddPlayBook |
| curator | ReadPlayBook, LoadPlayBook, ListTask, ReviewTask |
| executor | SubmitTask, ListTask, ReviewTask |

只读检索面(ListTask/ReviewTask)三席位皆有;写入面各归其位
(装载归 curator、提交归 executor、建任务/裁决/注记/注册新谱归 player)。

主对话只连 player 服务器,受限接口对它**不存在**;curator/executor 服务器由各自
subagent 的内联 mcpServers 拉起(见 [architecture.md](../architecture.md))。

## 凭证校验(checkKey,仅 curator/executor)

```text
env CHESS_SEAT_KEY 为空            → seat_denied/missing_env
.chess/run/seat.key 不可读         → seat_denied/missing_keyfile
两者不等(文件侧 TrimSpace)        → seat_denied/mismatch
任一失败:写 journal → 返回错误 → main 以非零码退出,不注册任何工具,不降级
```

认证发生在**进程出生时**且仅此一次——之后的每个 handler 都无需也不得再判身份。

## handler 外环(add)

每个工具经统一包装:

1. 取 `req.Params.Arguments`(空则 `{}`)→ 强类型解码(失败即结构化错误);
2. 调用领域函数;
3. 写 `tool_called` 日志:`tool, args(全量), ok, error_code?, duration_ms`;
4. 应答:成功 → 领域输出 JSON 同时放入 `Content[TextContent]` 与
   `StructuredContent`;失败 → `IsError=true` + `{code,message,data}` 文本。

错误模型约定(与 [interfaces.md](../interfaces.md) 通则一致):isError 只表调用方错误;
idle/未完成/终局等领域状态由 match 作为正常输出返回,本层不转译。

## 输入 schema

手写 JSON Schema 字面量(object/properties/required/additionalProperties:false),
required 由宿主侧校验兜底,handler 内仍有空值守卫(如 ReviewTask 的空 task_id)。

## 与 SDK 的关系

MCP SDK 触点收敛在本包(server 装配/运行)与 verify(一次性 client)、集成测试
(in-memory transport)三处薄封装,SDK API 变化时修改面最小。

## 测试要点

工具面断言(每席位 tools/list 与表一致);checkKey 三种拒绝路径;
集成:in-memory transport 直连三席位跑通双终局对局(该路径绕过 checkKey,
凭证另由专项测试与真实进程冒烟覆盖)。
