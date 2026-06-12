# 文档索引

`cipher-2` v1 是 FACT-only 运行时：通过类型驱动 Clang AST capability probe 后，按需使用 compile database per-file flags、process worker 并行和 partial AST 接受策略抽取 `TheFact`、`FactRelative` 和 source inventory，initializer 使用 worker-local exact relative dedup、SQLite-backed facts reducer、SQLite pending direct-call staging 和 relatives external k-way merge 按 id 去重、排序，并用只读函数索引并行后处理跨文件 `direct_call`；storage 使用 v5 `compact-jsonl-gzip` snapshot 和持久 `read_index.sqlite` 查询索引，read index schema v6 对 relatives endpoint 和 relative id 使用整数代理键投影，MCP 只公开 `search` 和 `detail`，其中 `search` 承载 FACT 分词、一跳关系、函数指针 dispatch 候选、有界传递闭包和有界可达性查询，tools/log 记录 `init.stage` 分阶段耗时，views 负责人类可读状态。

```text
type-driven Clang AST + compile command index + process workers + partial AST
  -> worker-local exact relative dedup
  -> SQLite-backed facts reducer + relatives external id merge
  -> parallel readonly-index direct_call resolver
  -> TheFact + FactRelative + source inventory
  -> compact storage snapshot + persistent proxy-key read index
  -> temporary incremental overlay
  -> MCP search/detail
  -> tools/log (`init.stage` + runtime events)
  -> tools/views (status model)
```

当前主线不保留 Graph projection、Inference rule framework、MCP `impact`、Graph scope、`.cipher/inference/` 工作区或交互式 inference setup。历史设计仍保存在 `docs/design-drafts/`，但运行时权威规格以模块 README 为准。

`initializer/extractor/code` 和 `storage` 现在都以 package 根 `__init__.py` 做兼容 re-export，实际实现按 backend、mapper、streaming、snapshot、read index、search/view 等职责拆到子模块；这只是结构拆分，不改变 v1 数据流、snapshot schema、CLI/MCP/config 或 Python public API。

## 模块 README

| 路径 | 内容 |
|---|---|
| `../README.md` | 项目范围、快速开始和运行时约束。 |
| `../src/README.md` | `src/` 目录职责和包边界。 |
| `../src/cipher2/README.md` | 主 Python 包、CLI、数据流和包级契约。 |
| `../src/cipher2/config/README.md` | `.cipher/config.yml`、toolchain、incremental 配置和路径安全。 |
| `../src/cipher2/initializer/README.md` | init/rebuild 编排、Clang 抽取和 snapshot 写入。 |
| `../src/cipher2/initializer/extractor/code/README.md` | C 语言类型驱动 Clang AST facts、relatives、compile database per-file flags、partial AST 接受、跨文件 direct_call 后处理、宏/位运算/包装表达式 field access、函数指针 dispatch、field fact 覆盖、object identity、capability fail-closed 和文件级 best-effort。 |
| `../src/cipher2/storage/README.md` | gzip FACT snapshot、持久 read index、FactView、multi-term search、关系 BFS、relative preview 和 overlay。 |
| `../src/cipher2/storage/schema/README.md` | v5 gzip 文件 schema、read index、manifest、stats 和兼容策略。 |
| `../src/cipher2/mcp/README.md` | stdio MCP `search` / `detail` 工具、schema、关系谓词、传递闭包、可达性、预算、relative preview 质量选择和错误语义。 |
| `../src/cipher2/incremental/README.md` | 在线临时增量 overlay。 |
| `../src/cipher2/tools/log/README.md` | JSONL 事件、摘要、MCP response budget / relative preview quality 统计和脱敏规则。 |
| `../src/cipher2/tools/views/README.md` | storage/log/incremental view model 和 MCP response budget / relative preview quality 呈现。 |
| `../scripts/README.md` | 测试和性能门禁脚本。 |
| `../benchmarks/README.md` | 手动评测工具目录。 |
| `../benchmarks/retrieval/README.md` | 检索质量评估 harness、manifest、preview/full 口径和弱模型 A/B 协议。 |
| `../tests/README.md` | 测试矩阵和覆盖要求。 |

## 用户与维护文档

| 路径 | 内容 |
|---|---|
| `user-guide.md` | 面向使用者的初始化、配置、MCP 查询和排障说明。 |
| `cipher-agent-system-prompt.md` | 可附加到消费方 agent 系统提示的 cipher 使用指导。 |
| `maintenance-guide.md` | 面向维护者的开发流程、PR gate、测试门禁和发布检查。 |
| `schema.md` | FACT、FactRelative、source inventory 和 v5 compact snapshot/read index 文件说明。 |
| `design-drafts/README.md` | 设计草稿索引和设计到实现的流程约束。 |

## 文档流程

任何新功能或改变规格的 bug 修复都必须先写设计草稿并提设计 PR。设计 PR 合入后，将草稿内容搬迁到对应模块 README 并递归更新到顶层文档；README 搬迁 PR 合入后，才能按 TDD 实现代码。实现完成后仍需提 PR，PR 合入视为维护者确认。
