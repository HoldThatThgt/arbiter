# 设计草稿

## 路径职责

本目录存放功能开发前的设计草稿，方便用户在文件中审阅和评论。

## 使用流程

每个新功能或改变规格的 bug 修复，都必须先创建 `YYYYMMDD-主题.md` 草稿。草稿至少覆盖模块定位、规格约束、数据结构、对外接口、并发控制、递归文档更新、可观测性、可观测用例看护和测试门禁。

## 草稿索引

- `20260525-log-tool.md`：`tools/log` 追加式 JSONL 日志。
- `20260525-storage-fact-store.md`：storage FACT-only file store。
- `20260526-tools-views.md`：`tools/views` view model。
- `20260526-config-runtime.md`：config 仓库本地配置。
- `20260526-initializer-code-runtime.md`：initializer 编排和 v1 代码事实抽取。
- `20260526-fact-relative-runtime.md`：FACT_RELATIVE 运行时关系层和 C 语言 Clang AST-backed extractor。
- `20260526-temporary-incremental-update.md`：在线临时增量 overlay 与离线全量重建。
- `20260527-graph-runtime.md`：Graph projection 运行时、Graph relation 和证据链历史草稿；已被移除 Graph/Inference/impact 设计废弃。
- `20260527-inference-rule-framework.md`：用户可声明 inference rules 与 Graph patch 物化框架历史草稿；已被移除 Graph/Inference/impact 设计废弃。
- `20260527-cli-interactive-inference-setup.md`：`init/rebuild` 交互式工程准备、`.cipher/inference/` 规则工作区和规则填写教程历史草稿；已被移除 Graph/Inference/impact 设计废弃。
- `20260527-remove-graph-inference-impact.md`：移除 Graph/Inference/impact，恢复 FACT-only search/detail，并新增 search 分词与 field_access 关系；已搬迁并实现，权威规格见模块 README。
- `20260527-toolchain-capability-probe.md`：放宽 Clang/GCC 版本硬锁，改为 Clang AST JSON capability probe；已搬迁并实现，权威规格见模块 README。
- `20260527-file-level-clang-best-effort.md`：文件级 Clang AST 失败改为 best-effort warning；已搬迁并实现，权威规格见模块 README。
- `20260527-cli-status-command.md`：新增 `cipher2 status` 只读命令，用于呈现 storage/log/incremental 统计；已搬迁并实现，权威规格见模块 README。
- `20260528-type-driven-ast-object-id.md`：类型驱动 AST 提取与 object_id 重设计，覆盖 source 归属和 field 命名；已搬迁并实现，权威规格见模块 README。
- `20260528-field-fact-coverage.md`：匿名 `struct/union`、嵌套 record 和未解析 type 的 field fact 覆盖补全；已搬迁并实现，权威规格见模块 README。
- `20260529-field-access-expression-recursion.md`：宏、位运算和包装表达式中的 `MemberExpr` 递归扫描，提升 `field_read` / `field_write` 覆盖；已搬迁并实现，权威规格见模块 README。
- `20260529-macro-function-pointer-dispatch.md`：宏展开 direct call 与函数指针 field/global/function_pointer_slot dispatch 抽取，补齐 `assigned_to` / `dispatches_via`；已搬迁并实现，权威规格见模块 README。
- `20260529-detail-relative-preview-quality.md`：`detail.relative_preview` 桶内 call-site rollup、salience 排序和 endpoint source 多样化选择；已搬迁并实现，权威规格见模块 README。
- `20260529-detail-relative-endpoint-labels.md`：在 #91 已有 endpoint_name/source 基础上，为 `detail.relative_preview` shown relative 补充 endpoint profile；已搬迁并实现，权威规格见模块 README。
- `20260529-retrieval-retest-and-weak-model-ab.md`：呈现和覆盖修复后的检索可还原率复测，以及大库弱模型 grep vs `+cipher` A/B 验证；已搬迁并实现，真实大库报告需人工运行。
- `20260530-search-relational-predicates.md`：`search.query` 增加 readers/writers/accessors/callers/callees 关系型谓词、`file:` / `caller:` 过滤和 `too_broad` 可执行精化提示；已搬迁到模块 README 并实现。
- `20260530-system-prompt-agent-guidance.md`：随仓库分发可附加到消费方 agent 系统提示的 cipher 使用指导，约束弱模型在关系查询和 `too_broad` 场景中信任 indexed FACT view、避免 grep/name 猜测/自写 parser 补全；已搬迁到 docs 并实现。
- `20260530-bounded-transitive-search.md`：`search.query` 增加有界 callers/callees 传递闭包和 `reachable:A->B`，含 slim relation 输出、成本预算和可达性完整性语义；设计 PR #131 已合入，README 已搬迁，TDD 实现随 #127 待合入。
- `20260602-init-progress-stderr.md`：`init` 期间向 stderr 展示 source 进度、当前文件、耗时、warning/partial AST 和 compile database hit/miss，复用 `extractor.code.file` 发射点；Part of #162。
- `20260603-global-object-id-determinism.md`：修复 `global` fact identity 依赖 `ordinal` / translation unit 上下文导致同输入 init 不可复现；设计阶段，关联 #196。
- `20260605-libclang-ast-timeout.md`：恢复生产 in-process libclang target AST 单文件 timeout，设计 managed worker kill/restart 以产出 `clang_ast_failed` timeout warning；设计阶段，关联 #224；review staging 同步见 `../design_draft`。
- `20260606-live-libclang-smoke-fixtures.md`：为 #226 增加 toolchain-gated 真实 ctypes libclang smoke 测试设计，覆盖 JSON test backend 盲区；README 已搬迁，TDD 实现中。
- `20260528-compile-db-per-file-flags.md`：读取 `compile_commands.json` per-file 编译参数并用于 Clang AST invocation；已搬迁并实现，权威规格见模块 README。
- `20260528-cross-file-direct-call-resolution.md`：在 Clang AST 文件级抽取后，用 pending call evidence 后处理拼接跨文件 `direct_call` relation；已搬迁并实现，权威规格见模块 README。
- `20260528-partial-clang-ast.md`：接受 Clang returncode/stderr 报错但 stdout 仍有效的部分 AST，保留可抽取 facts 并以 `partial_ast` warning 观测；设计 PR #76 已合入，权威规格已搬迁到模块 README，代码实现已合入。
- `20260528-storage-compact-snapshot.md`：storage snapshot 从明文 JSONL 改为 deterministic gzip JSONL，降低大型仓库磁盘占用；已搬迁并实现，权威规格见模块 README。
- `20260528-storage-persistent-read-index.md`：storage snapshot 写入持久 `read_index.sqlite`，降低 MCP/查询冷启动耗时；Issue #55 唯一设计草稿，后续修订必须在本文件内完成；已搬迁到模块 README 并实现。
- `20260529-retrieval-quality-harness.md`：将检索质量评估 harness 落地到仓库，提供离线确定性的 coverage、retrieval_probe 和分析工具；已搬迁并实现，真实大库报告需人工运行。
- `20260602-retrieval-usage-observability-schema-v2.md`：检索取用可观测性 schema v2，补全 `mcp.search.returned_ids`、`mcp.detail.subject_id=fact_id`，删除 `query_sha256`，并保留 `base_snapshot_id`；Part of #156。

设计草稿必须单独提 PR；设计 PR 合入后，草稿内容才能搬迁到对应模块 README，并按影响范围递归更新到顶层文档。README 搬迁也必须单独提 PR；文档搬迁 PR 合入后才能进入 TDD 开发。

## 约束

- 草稿不是最终规格来源；设计 PR 合入后必须搬迁到对应 README。
- 草稿搬迁前不得写运行时代码、测试或迁移文件。
- 草稿搬迁后、文档搬迁 PR 合入前，仍不得写运行时代码、测试或迁移文件。
- 任一 PR 有检视意见时，必须先按意见修订同一个 PR。
