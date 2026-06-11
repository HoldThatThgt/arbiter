# 安全边界

Chess 的隔离目标是**约束协作纪律、防止主模型偷懒与误用**,不是对抗恶意模型的安全边界。
本文说明机制分层、威胁模型与残余通道,以及可选硬化。

## 用 RBAC 描述这套门禁

- **授权** = 席位工具表(role → 接口绑定):player 7 个、curator 4 个、executor 3 个,
  只读检索面(ReviewTask/ListTask)三席位皆有。
- **认证**不是任何模型可调用的声明,而是两件宿主掌控的事实:
  1. **连接来源**:宿主按配置接线——player 服务器来自 `.mcp.json`,curator/executor
     服务器内联在各自 agent frontmatter 的 `mcpServers` 中,只在该 subagent 运行期间
     挂载、主对话不可见。受限接口在主对话工具清单里**根本不存在**。
  2. **席位凭证**:`chess init` 生成随机 `seat.key`(16 字节,32 hex 字符)存于封禁的
     `.chess/run/`,并经 curator/executor 配置的 `env` 注入 `CHESS_SEAT_KEY`。特权席位
     启动时比对 env 与文件,不一致即写 `seat_denied` 日志(reason: missing_env /
     missing_keyfile / mismatch)、拒绝服务并以非零码退出。player 席位不设凭证
     ——其工具面本就授予棋手。

为什么不是 deny 规则或 hook:宿主的 `permissions.deny` 与 `disallowedTools` 对 subagent
同样生效(拦棋手必然拦死 curator/executor);PreToolUse hook 输入不含调用方身份字段。
为什么不是 ClaimRole 式声明接口:主/子 agent 是宿主进程内的会话循环,无独立 PID,
且 MCP 连接内无可信调用方字段——模型可调用的接口只能承载声明,承载不了认证。
详见 [decisions.md](decisions.md)。

文件侧:`.chess/playbook`、`.chess/run` 与两个 agent 文件(内含凭证)以路径 deny 规则
全局封禁。没有任何席位需要用宿主文件工具读它们:裁判进程是独立 OS 进程不受宿主权限
约束,由它代为读取;curator 经 ReadPlayBook 通读;agent 文件由宿主加载器读取。

## 威胁模型与残余通道

| 路径 | 状态 |
|---|---|
| 棋手直接调用 ReadPlayBook/LoadPlayBook/SubmitTask | **不存在该工具**(构造性隔离) |
| 棋手用 Read/Glob/Grep 及宿主识别的只读命令读棋谱/状态/凭证 | deny 规则拒绝 |
| 棋手用 Bash 自行拉起特权席位进程并以 JSON-RPC 对话 | 缺 `CHESS_SEAT_KEY`,启动即 `seat_denied` 拒绝服务 |
| 棋手自写挂内联 mcpServers 的 agent 文件再召唤 | 同上:伪造配置拿不出凭证 |
| 棋手用任意脚本/解释器读封禁文件窃取凭证、或直改 state | **残余**:宿主路径规则不覆盖任意子进程;此为剩余主要通道,动作显式且全程留痕 |
| 棋手经 git 读已提交文件绕过路径 deny | curator 文件不入库(init 写 .gitignore);executor 文件入库与否由用户决定,提交凭证即接受该残余 |
| 棋手改 settings.json 删 deny | **残余**:模型改自身配置,与窃密同属显式违规 |

本质边界:**主模型与席位进程同属一个 OS 用户,同用户内不存在绝对的用户态安全边界**
——任何凭证最终放在 Bash 可读的地方。凭证层的价值是把绕行成本从"一条命令"提高为
"无可辩解、全程留痕的显式窃取",并使 `seat_denied` 成为检测信号。

残余通道的缓解:chessplay 规程铁律(行为约束)+ journal 全量留痕(事后审计)+
"无需求即无动机"(按步即可完成工作)。

## 可选硬化(需要硬边界的用户)

启用宿主沙箱的文件系统读拒绝清单,覆盖 `.chess/playbook`、`.chess/run` 与两个 agent
文件(结构化配置,非命令串匹配)。OS 级拒绝对任意子进程同样生效,可封死上表唯一的
窃密残余:Bash 拉起的冒名席位连状态/棋谱都读不到,fail closed;合法席位由宿主拉起,
不在 Bash 沙箱内,不受影响。Chess 默认不启用,保持部署最小。

## 停止门控与棋谱写入面(AddPlayBook / NotePlaybook)的边界

- **Stop hook 是行为门控,不是安全边界**:`chess hook stop` 按对局状态决定放行/拦截,
  锁忙或自身故障时 fail-open(放行停止,错误进 stderr)——宁可少拦一次,不卡死会话。
  拦截以回合为窗口计数,超过 32 次说明模型原地打转,以 `aborted/stop_limit` 终局并
  放行。用户中断不经 hook;同仓库多 session 共享同一门控(单活动对局语义的推论)。
- **AddPlayBook 暴露给棋手但只增不改**:注册新棋谱不泄露任何既有棋谱内容;同名即
  `name_conflict` 拒绝(且错误不附目录清单);文件名经路径等值校验防穿越;全文进
  journal 留痕。棋手理论上可自创棋谱再请 curator 装载——这属于"模型给自己定流程",
  由 journal 可审计性与 curator 的场景适配判断兜底,符合纪律性隔离的定位。
- **NotePlaybook 是受约束的单点写通道**:只能向**已装载对局**的棋谱、**本局已走过**
  的步骤追加单行注记(≤ 1024 字节,`- ` 前缀落入 `[Gotcha]` 节)——改不了
  job/checklist/branch/goal,创建/覆盖不了文件,也探测不到未走过的步骤与其他棋谱
  (棋手可见信息仍 = ShowStepJob ∪ 历史);写盘前整体重解析复核,结构破坏即拒写;
  每条注记全文进 journal(`playbook_noted`)。残余:注记是给未来对局看的提示文本,
  模型可借它跨对局传话(与棋谱本体同级的提示注入面)——注记只增不删、全程留痕,
  按"定期人工复查 [Gotcha] 节"对待(见 playbook-format.md)。

## 信任说明(验证谓词)

两种谓词都由 executor 编写、以用户本机权限运行(shell 直接执行;mcp 拉起的是用户
自己在 `.mcp.json` 声明过的服务器),信任级别与宿主自身工具等同;全部谓词与输出进
journal 可审计。`reserved_server` 校验(目标 command 与 Chess 自身二进制路径相等即拒绝)
防止验证流程递归调用或改写对局状态。谓词的"质量"(是不是真验证)取决于棋谱
checklist 与用户 executor 的素质——Chess 保证的是**每个任务都有一条机器可裁决的谓词**。
