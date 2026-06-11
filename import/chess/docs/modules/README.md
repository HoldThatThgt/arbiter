# 模块设计文档

| 模块 | 职责一句话 |
|---|---|
| [playbook.md](playbook.md) | 棋谱文法、行式分词解析、校验、目录扫描;全局错误码与常量的家 |
| [match.md](match.md) | 对局状态机:flock+原子写、两段式提交、确定性裁决、status 投影 |
| [verify.md](verify.md) | 验证谓词(shell/mcp)的校验与执行,任务成败的唯一事实来源 |
| [journal.md](journal.md) | append-only JSONL 行为日志,事件总表 |
| [seat.md](seat.md) | 三席位服务装配、席位凭证校验;"席位即权限"的实现点 |
| [deploy.md](deploy.md) | chess init:模板渲染与宿主配置结构化合并;宿主路径唯一所在地 |

依赖方向:`seat → match → verify → deploy(仅 MCPConfigPath)`,所有包 → `playbook`(模型与常量)、`journal`(日志)。
