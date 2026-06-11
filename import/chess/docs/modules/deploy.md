# 模块设计:internal/deploy

一键部署(`chess init`):模板渲染与宿主配置的结构化合并。
**全项目唯一允许出现宿主路径常量的包**(`.mcp.json`、`.claude/...`)。

## 职责与边界

- `Init(root) (guidance, error)`:幂等地把 Chess 部署进目标仓库,返回打印给用户的
  后续指引(含 executor 契约模板与本机凭证)。
- `MCPConfigPath(root)`:向 verify 提供 `.mcp.json` 路径(宿主路径不外溢的方式)。
- **不做**:删除/迁移用户数据,触碰 `chess-executor.md`(用户财产)。

## Init 八步(全部幂等)

| # | 动作 | 策略 |
|---|---|---|
| 1 | 建 `.chess/playbook|run|log` 与 `.claude/agents|skills` 目录,写 `.chess/FORMAT.md` | 目录存在跳过;FORMAT 仅缺失时写入,**置于棋谱目录之外**(避免成为 ReadPlayBook invalid 噪音) |
| 2 | `ensureSeatKey`:`.chess/run/seat.key`,16 字节 crypto/rand → 32 hex,0600 | 已存在且形态合法则沿用(重跑不轮换);缺失则新生成 |
| 3 | 合并 `.mcp.json`:`mcpServers.chess = {stdio, <绝对路径>, [serve, player]}` | map[string]any 读改写,保留未知字段;`chess` 键归 Chess 所有,既有条目指向不同 command 时覆盖并在 guidance 提示 |
| 4 | 写 `chess-curator.md`(模板渲染:二进制路径 + 凭证 env) | Chess 拥有,覆盖刷新,0600 |
| 5 | 写 `chessplay/SKILL.md` 与 `playbook-create/SKILL.md` | Chess 拥有,覆盖刷新 |
| 6 | 合并 `settings.json`:`permissions.deny` 并集追加 4 条 + 注册 Stop 门控 hook(`<exe> hook stop`) | 结构化合并;hook 条目以"命令尾词 = hook stop"认领并刷新二进制路径,幂等不重复,其他 hook 原样保留 |
| 7 | 追加 `.gitignore` 4 行(run/、log/、status.json、chess-curator.md) | 整行存在性检查;凭证与机器路径不入库 |
| 8 | 返回 guidance:剩余两件事(放棋谱、建 executor)+ 含凭证的 executor 完整模板 | — |

二进制路径取 `os.Executable()` 后 Abs + EvalSymlinks,三个席位保证同一二进制
(也是 verify 的 reserved_server 判定基准)。

## 关键实现

- **结构化合并**(readJSON/writeJSON):整文件解码为 `map[string]any`,只触碰自己的
  键,序列化回写——用户的任何既有字段原样保留。文件不存在/空文件视作空对象。
- **原子写**(atomicWrite):CreateTemp + Write + Chmod + Sync + Rename,失败清理。
- **模板**:`go:embed templates/*`(curator agent、chessplay skill、FORMAT.md);
  `render` 只做两个占位符的整串替换(`{{CHESS_BIN}}`、`{{SEAT_KEY}}`)。
- 凭证轮换语义:seat.key 缺失重生成时,第 4 步自然把新凭证刷进 curator 文件;
  executor 文件归用户,guidance 提示手动同步 env(配套排查见 manual 第 8 节)。

## 模板内容契约

- `chess-curator.md`:tools 白名单锁三工具;指令要求通读全文选谱、最终消息只报
  名称/理由/入口、不泄露棋谱内容、报错原样转述。
- `chessplay/SKILL.md`:棋手操作规程——开局(召唤 curator)、回合循环
  (ShowStepJob 含 gotchas → 按 checklist 建任务 → 派 executor → ReviewTask 深查 →
  NotePlaybook 记坑 → CheckStepJob)、终局汇报(ListTask 通览 summary),
  以及铁律(不碰封禁路径、不找替代途径、不亲自执行、executor 故障原样上报)。
- executor 模板(guidance 打印,不落盘):frontmatter 含内联 mcpServers + 凭证 env +
  工具白名单;指令要求完成后必须 SubmitTask,附一句话 summary 并给出可独立验证的谓词。

## 测试要点

合并 golden(空仓库;含无关字段的 .mcp.json/settings.json 不丢数据);重复 init
幂等(文件树快照一致);seat.key 沿用与缺失重生成;既有 chess 条目指向不同 command
时 guidance 含覆盖提示。
