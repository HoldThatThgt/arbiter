# 模块设计:internal/verify

验证谓词的定义与执行:任务成败的唯一事实来源。

## 职责与边界

- 定义 `ResultSpec`(谓词)与 `Result`(执行结局),校验、归一化、执行、判定。
- **不做**:状态读写(match 负责)、锁(调用方保证在锁外执行)。

## 核心类型

```go
ResultSpec { Kind("shell"|"mcp"), Command,            // shell
             Server, Tool, Arguments,                  // mcp
             TimeoutS(默认600,≤3600), OutputLines(默认256,≤10000) }
Result     { Spec(归一化后), ExitCode *int, IsError *bool,
             Output(尾部N行), DurationMS, Failure("timeout"|"spawn_error"|"transport_error") }
SpecError  { Code, Message, Data }    // 前置校验错误(bad_result 等,调用方错误)
```

判定函数 `Pass(result)`:`Failure` 非空 → false;否则 shell 看 `*ExitCode==0`,
mcp 看 `!*IsError`。**前置校验失败是接口错误(isError),执行期失败是正常 fail 结局**
——这是错误模型的关键区分:谓词写错该改谓词,谓词跑挂该由棋手分析。

## shell 形态(runShell)

- `/bin/sh -c <command>`,工作目录 = 仓库根,继承环境;`Setpgid` 进程组隔离。
- 超时:context 到期对**整个进程组** `SIGKILL`(负 pid),`Failure="timeout"`
  ——管道里 fork 出的子孙进程一并清理。
- 输出:stdout+stderr 合并进 `capBuffer`(1 MiB 硬上限,超出静默丢弃但不阻塞写端),
  结束后 `tailLines` 保留尾部 OutputLines 行。
- exit code 经 `exec.ExitError` 结构化提取;无法启动 → `spawn_error`。

## mcp 形态(runTool)

```text
readServerConfig: 结构化解码仓库根 .mcp.json → mcpServers[server]
  不存在        → server_not_found      (前置,isError)
  type != stdio → unsupported_transport (前置,isError)
  command 解析后与 os.Executable() 相同 → reserved_server(前置,isError)
执行: 一次性拉起目标服务器进程 → SDK client 初始化 → tools/call → 关闭
  初始化/调用传输失败 → Failure = transport_error(或 ctx 超时 → timeout),正常 fail
  调用完成 → IsError = 应答标志,Output = content 序列化 JSON 的尾部 N 行
```

- `reserved_server` 用**路径相等**判定(LookPath/Abs/EvalSymlinks 后比较),不做名称
  比较——防止验证流程递归调用 Chess 自身席位、在验证中改写对局状态。
- 每次验证一次性进程(拉起→调用→退出),无连接池、无常驻;`.mcp.json` 路径常量
  从 deploy 包引用(宿主路径只存在于 deploy)。
- 仅支持 stdio(v1);整体共用 TimeoutS 期限。

## 不变式

- 归一化先于校验先于执行(`Execute` 内部顺序),零值字段拿到默认值,越界拿到
  `bad_result`,合法谓词必然产出带 Verdict 依据(ExitCode 或 IsError 或 Failure)的 Result。
- 任何执行路径的 Output 都经过同一截断(行尾部 + 1 MiB 硬上限),状态与日志体积有界。

## 测试要点

shell:exit 0/非 0、超时杀进程组、不可启动、行截断与硬上限;
mcp:桩服务器 pass / isError / 传输中断 / 超时,三个前置校验各一例;
timeout_s / output_lines 按次覆写与越界 bad_result。
