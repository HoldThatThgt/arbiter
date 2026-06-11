# 设计决策记录

记录"为什么是这个形态"。除明确标注外,均为 v1 定版决策。

## 设计原则

1. **最小化用户交互**:`chess init` 一条命令完成部署,运行期用户无需操作。
2. **最简化模型交互**:10 个接口、单接口单语义;查询面三层定型——ShowStepJob 局面总览、ListTask 任务索引、ReviewTask 单任务细节,无其他查询面。
3. **最大化用户可观察性**:journal 全量行为日志 + status.json 实时局面。
4. **极致性能与小型化**:单一静态二进制、毫秒级席位进程、KB 级状态文件,无守护进程/网络/数据库。
5. **功能简洁**:不做持久化兼容、多对局并发、远程协作、Windows(v1)。
6. **结构化边界**:运行期判定只消费结构化字段,自然语言与正则/字符匹配永不参与;全仓库无 `regexp`。

## 关键决策

1. **席位拆分服务器实现门禁**:同一二进制按 `serve <seat>` 只注册该席位工具表。
   受限接口对主对话"不存在"而非"被拦截"。否决 deny/hook 方案的依据见文末宿主事实。
2. **状态放文件 + flock,席位进程无内存状态**:curator/executor 进程随 subagent 启停、
   与 player 进程内存不共享;常驻共享进程违背小型化。代价是每次调用一组文件 IO
   (毫秒级),收益是天然多进程安全与免费的可观察性。
3. **棋谱快照语义**:装载时一次性解析入 state,对局期间不读源文件——确定性优先,
   中途编辑棋谱不影响进行中对局。state 含未来步骤故放封禁目录,status.json 只投影
   当前与历史(棋手可见信息 = ShowStepJob ∪ 历史)。
4. **checklist 不做机器核对**:语义覆盖核对必然要文本匹配,违反结构化边界。覆盖性
   是棋手的分析职责(chessplay 规程强制"每条 checklist 至少一个任务覆盖"),机器只
   裁决"已建任务是否全部验证通过"。两层合并构成完整核对。
5. **每回合至少一个任务**才可能裁决完成:"棋手不干脏活"的机器化表达——任何步骤的
   落地动作(哪怕只是"确认 X 成立")都必须经 executor 提交一条可验证谓词。
6. **result 谓词为 kind 判别的结构化二选一**(shell | mcp):shell 看 exit code,mcp 看
   isError;超时与截断为谓词级可配字段(timeout_s/output_lines)。mcp 形态仅解析仓库根
   `.mcp.json`、仅 stdio;`reserved_server` 以二进制路径相等判定(非名称比较),防验证
   流程递归/改写对局状态。
7. **ReadPlayBook 与 LoadPlayBook 分离**(单接口单语义):前者无参只读返回全部棋谱
   完整内容(curator 必须通读全文才能选谱,且棋谱目录对宿主文件工具封禁,这是唯一
   通道);后者 name 必填只负责装载。两者都只在 curator 席位注册。
8. **ReviewTask/ListTask 全席位注册、只读**;ShowStepJob 相应瘦身为局面总览
   ——局面、索引、细节三层分离,局面应答不随验证输出膨胀。ListTask 行只含
   编号/回合/步骤/状态/summary,索引有用的前提是 summary 必填(见 20)。
9. **任务可重交,最后一次为准**:重试不需要新接口;放弃即带着 fail 请求裁决走失败
   分支。重试策略由棋谱 Branch 设计 + 棋手分析表达,裁判不藏隐式重试。
10. **单仓库单活动对局**:重复装载即替换(journal 记 match_replaced);不设手动中止
    接口,换棋谱重开即事实中止。
11. **executor 命名约定固定 `chess-executor`**(约定优于配置):棋手必须能不经询问找到
    执行席位。executor 由用户提供;未提供/配置错误(含缺凭证 env)导致对局停在
    open 任务上,属用户责任,棋手按规程原样上报。
12. **席位凭证而非声明式角色绑定**:否决过 ClaimRole(pid, role) 式接口——主/子 agent
    是宿主进程内的会话循环,无独立 PID;MCP 连接内无可信调用方字段,声明只能由被
    约束方自证。采用宿主注入 env 凭证 + 特权席位启动期比对:认证发生在进程出生时,
    零新增模型可见接口。同一 OS 用户内无绝对边界,该层定位为"抬高门槛 + 留痕检测"
    (硬边界见 security.md 沙箱项)。凭证取 16 字节随机(32 hex 字符),128-bit 熵对
    该威胁模型足够。
13. **SubmitTask 两段式锁协议**:谓词执行(可能数分钟)在锁外,回锁以 round_seq 复核
    回合未推进,变化则 task_stale 作废——长验证不冻结棋局,迟到结果不污染新回合。
14. **FORMAT.md 置于 `.chess/`(棋谱目录之外)**:否则它会成为 ReadPlayBook `invalid`
    列表的固定噪音(实现审计中发现并修正)。
15. **isError 仅表调用方错误**:idle/未完成/已终局是正常结构化结果;谓词的超时/启动
    失败/传输失败也不是调用方错误,而是 verdict=fail + failure 原因,留给棋手分析。
16. **[SetGoal] 即 checkmate 判据**:声明后,每次成功裁决都在锁外执行 goal 谓词
    (两段式,期间重交会触发重算),通过即无视分支立即胜局——将死可以发生在任何
    一着;到达 END 而 goal 未过判负(goal 是唯一胜利判据,END 只是"无棋可走")。
    goal 与回到自身的单步棋谱组合 = "反复尝试直到通过"的标准形态。
17. **回合预算取代硬上限**:`max_steps` 默认 256、上限 1024、frontmatter 可配,耗尽
    → `aborted/steps_exhausted`;v1 的全局 128 与单步重入 32 两个写死上限废除——
    预算是唯一循环边界,否则单步重试型棋谱会先撞内部上限,作者无从知晓也无从配置。
18. **停止门控经宿主 Stop hook**(`chess hook stop`):active → 结构化拦截并附局面
    指引,终局 → 放行;每回合拦截 32 次后 `aborted/stop_limit`(无进展卡死保护,
    进入新回合清零);门控故障 fail-open;用户中断不经 hook。该层与席位隔离同属
    行为纪律,不是安全边界。
19. **AddPlayBook 注册在 player 席位、只创建不覆盖**:创建动作不泄露既有棋谱内容,
    故无需另设席位;文件名以 `filepath.Base` 等值校验防穿越;`name_conflict` 错误
    不附目录清单(不向棋手泄露棋谱名)。配套 playbook-create skill 引导
    访谈 → 起草 → 注册 → 按 issues 迭代。
20. **任务 summary 由 executor 必填**(SubmitTask 入参,非空、≤ 1024 字节,违例
    `bad_summary` 在谓词执行前拒绝):一句话结果概要是 ListTask 索引与终局汇报的
    素材,选填必然漏填,而 report 太长不堪索引。summary 只是检索文本,**永不参与
    裁决**(成败仍只看谓词结局),不破坏结构化边界。
21. **gotcha 注记进棋谱本体而非旁挂存储**(`[Gotcha]` 节 + NotePlaybook 文本手术):
    经验沉淀应跟随棋谱本身被版本管理、被 ReadPlayBook/装载天然携带,旁挂文件会
    分叉出第二事实源。注记是快照不可变性的**唯一例外**(双写源文件与快照,只增
    不改,写盘前整体重解析复核);限定单行、`- ` 前缀、仅本局走过的步骤,使追加
    既不可能破坏文法,也不扩大棋手的信息面(仍 = ShowStepJob ∪ 历史)。注册在
    player 席位:gotcha 是棋手的分析产物,executor 只见单任务、curator 不该改谱。

## 宿主机制事实(设计依据,2026-06 经官方文档核实)

| # | 事实 | 对设计的影响 |
|---|---|---|
| A.1 | `permissions.deny` / `disallowedTools` 全局生效,subagent 同样被拒,任何层级 deny 后无人可 allow | 否决"deny 受限接口"方案;路径 deny 可放心全局使用 |
| A.2 | PreToolUse hook 输入(session_id/cwd/tool_name/tool_input/…)无调用方席位字段 | 否决 hook 鉴别方案 |
| A.3 | subagent frontmatter 支持内联 `mcpServers`,仅该 subagent 连接、随其启停;这是按 subagent 限定 MCP 工具的唯一文档化机制 | 席位隔离的基石 |
| A.4 | `.mcp.json` 的 stdio 服务器为每 session 单实例,主对话与 subagent 共享 | player 席位常驻;curator/executor 进程独立 → 状态必须落盘共享 |
| A.5 | subagent `tools:` 为白名单,可按 `mcp__server__tool` 具名授权;省略则继承全部 | curator 锁三工具;executor 契约模板给显式白名单 |
| A.6 | `Read(path/**)` deny 覆盖 Read/Edit/Write 及宿主识别的只读命令,不覆盖任意子进程;宿主沙箱的文件系统拒绝清单可做 OS 级封禁 | 威胁模型残余通道与可选硬化的依据 |
| A.7 | `.mcp.json`、`.claude/agents/*.md`、`.claude/skills/*/SKILL.md`、`.claude/settings.json` 均免参数自动装载 | init 一次部署,打开即用 |

## 非目标

持久化数据版本兼容、Windows、多对局并发、远程/团队模式、棋谱编写工具、TUI、
对抗恶意主模型的安全边界(见 security.md)。

未来工作候选:mcp 谓词的非 stdio 传输(http)与用户级服务器来源、棋谱内声明步骤级
验证命令(把谓词从 executor 信任域上移到棋谱信任域)、status 的 TUI 旁观器、
对局归档检索、棋谱级常量覆写。
