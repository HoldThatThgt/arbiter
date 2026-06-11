# Chess

让 Claude Code 按你写的**棋谱(playbook)**办事。

主对话(棋手)只做分析与调度;脏活全部派给执行 subagent 完成,且每个任务必须附带一条
可机器裁决的验证谓词(shell 命令或 MCP 调用);步骤推进与成败由本地裁判进程按结构化
规则裁决——模型没有"宣布成功"的接口,只有"请求裁决"的接口。

```text
用户请求 → 棋手(主对话,分析/拆解/调度)
              ├─ chess-curator  通读棋谱并装载       (LoadPlayBook 只有它能调)
              ├─ chess-executor 执行任务并提交验证谓词 (SubmitTask 只有它能调)
              └─ 裁判进程       解析/状态机/核验/裁决/日志(确定性,无模型参与)
```

- **席位隔离**:受限接口在主对话的工具清单里根本不存在(构造性隔离),特权席位还需
  启动凭证,冒名拉起直接拒绝服务
- **可验证推进**:任务成败只看谓词的 exit code / isError,自然语言永不参与判定
- **checkmate 终局**:棋谱可声明 `[SetGoal]` 谓词——通过即整局胜利;配合 Stop 门控,
  **将死或回合预算(max_steps,默认 256)耗尽之前,模型无法自行停止**(用户中断不受影响)
- **棋谱可由模型起草**:`/playbook-create` 访谈 → 起草 → AddPlayBook 注册(只增不改)
- **棋谱越用越懂行**:对局中发现的坑经 NotePlaybook 沉淀进该步骤的 `[Gotcha]` 节
  (单行注记、只增不改、写前全量校验),下次走到这步随 ShowStepJob 自动返回;
  每个任务必须提交一句话 summary,ListTask 一眼通览全局、ReviewTask 按号深查
- **全程可观察**:`.chess/status.json` 实时局面 + `.chess/log/journal.jsonl` 全量行为日志
- **一条命令部署**:`chess init`,打开 Claude Code 即用
- 单一静态二进制,无守护进程、无网络、无数据库;macOS / Linux

## 快速开始

```sh
# 1. 安装(任选 PATH 目录;依赖已 vendor 入库,构建全程离线,内网机器可直接执行)
git clone git@github.com:HoldThatThgt/chess.git && cd chess
go build -o chess ./cmd/chess && sudo mv chess /usr/local/bin/

# 2. 在你的目标仓库部署
cd /path/to/your-repo
chess init

# 3. 放入棋谱、创建执行席位(整段复制即可,见用户手册第 3、4 步)
# 4. 开始
claude
> /chessplay 按流程修复构建失败
```

完整的傻瓜式步骤(含可直接复制的棋谱与执行席位模板、权限弹窗优化、故障排查):
**[docs/manual.md](docs/manual.md)**

## 文档索引

| 文档 | 内容 |
|---|---|
| [docs/manual.md](docs/manual.md) | 用户手册:安装→部署→写棋谱→建执行席位→开局→旁观,全程复制粘贴 |
| [docs/architecture.md](docs/architecture.md) | 总体架构:席位隔离、进程与状态模型、一局的生命周期 |
| [docs/playbook-format.md](docs/playbook-format.md) | 棋谱格式规格:文法、校验规则、编写建议 |
| [docs/interfaces.md](docs/interfaces.md) | 10 个 MCP 接口的入参/返回/错误码,常量总表 |
| [docs/security.md](docs/security.md) | 安全边界:RBAC 机制、席位凭证、威胁模型与可选硬化 |
| [docs/decisions.md](docs/decisions.md) | 设计决策记录与宿主机制事实(为什么是这个形态) |
| [docs/modules/](docs/modules/) | 六个源码模块各自的设计文档 |

## 开发

```sh
go build ./...            # 构建(依赖已 vendor,自动离线,无需访问任何代理)
go test -race ./...       # 全量测试(单元/进程/并发/集成)
go mod vendor             # 升级依赖后重新 vendor 并提交
```

全部依赖源码随仓库提交在 `vendor/`(约 3.4 MB):检测到 vendor 目录后 Go 自动以
vendor 模式构建,不发起任何网络请求——内网/离线环境开箱即用。

源码模块:`playbook`(棋谱解析)、`match`(对局状态机)、`verify`(谓词执行)、
`journal`(行为日志)、`seat`(席位服务装配)、`deploy`(一键部署)。
约定:全仓库无正则、运行期判定只消费结构化字段;宿主路径常量仅存在于 `deploy`。
