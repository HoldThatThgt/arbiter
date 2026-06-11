# tests

## 路径职责

本目录存放 `cipher-2` 的单元测试、集成测试、覆盖矩阵和工具链 fixture。测试必须围绕中文 README 的规格编写，先写失败用例，再实现代码。

## 基础命令

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

## 覆盖目标

新功能点覆盖率 100%，异常分支覆盖率 90%+，场景用例覆盖率 100%。涉及性能或小型化的改动必须覆盖 512MB、4GB、8GB 三档门禁。

## 当前主线测试范围

### CLI

- `init` / `rebuild` 成功和失败。
- `--json`、`--no-log`、`--source-root`、`--profile`、`--compile-database`、`--no-mcp-config`、`--print-mcp-config`。
- `init` setup：自动探测 compile database 优先级/未发现 warning、repo-root `.mcp.json` 新建/merge/幂等/malformed 不覆盖、MCP print fallback、build readiness 预检事件、toolchain report-only、外部 client 配置和 toolchain writeback flag 不被支持。
- `init` stderr 进度、`--no-progress`、TTY 原地刷新、非 TTY 周期整行、stdout JSON 纯净。
- `status target`、`status target --json`、missing target、unknown flag、invalid target。
- `status` human 输出 storage/log/incremental 三 section、empty snapshot、section error、无 ANSI、无 traceback。
- `status` JSON 输出完整 `ToolsOverviewModel`；views section error 不导致整个命令失败。
- `--interactive` 删除后的 usage error。
- CLI log 和 path safety。

### Config

- 带注释默认配置和写读往返。
- toolchain、compile database path、libclang last-resort library path、incremental ranges。
- `.cipher/` path safety。
- 旧 `graph.*` / `inference.*` 被忽略并写 warning。

### Initializer / Extractor

- 空仓库、单 C 文件、多 C 文件、header、include、macro。
- 源码布局红线：`src/` 下每个 Python 代码文件保持 < 2000 LOC，超线时必须按职责拆分。
- function/global/type/field/function_pointer_slot facts。
- direct_call、assigned_to、dispatches_via、has_field。
- fake toolchain fixture 输出 type-driven AST 必需字段；测试专用 JSON adapter 只能作为 parity oracle，正式路径不得回退到 JSON dump；`tests.test_live_libclang_smoke` 必须清空 JSON adapter、端到端运行真实 `_LibclangAstBackend` 和公开 initializer 写 snapshot 路径；本机缺 Clang/libclang capability 时 skip，CI 默认要求该 smoke 实际执行。
- capability probe 缺失 `loc.file`、call reference、member reference、`qualType` 时 fail-closed。
- libclang unavailable、自动探测失败、last-resort 显式库路径、版本不匹配、required unsupported symbol / C API 错误时 fail-closed；非稳定 opcode helper 缺失时必须继续运行，并用稳定 `clang_tokenize` 保持 assignment / compound assignment / `++` / `--` 的 field access parity。
- libclang cursor kind 归一：`MemberRefExpr -> MemberExpr`、`StructDecl/UnionDecl -> RecordDecl`、`ClassDecl -> CXXRecordDecl`、`ParmDecl -> ParmVarDecl`、`CXXMethod -> CXXMethodDecl`、`UnexposedExpr -> ImplicitCastExpr`；真实 backend smoke 必须覆盖 struct field fact、has_field、field_read/field_write、direct_call、header static inline canonical source、`function_pointer_slot` / `assigned_to` / `dispatches_via`、同名 `static` identity 和匿名 union field；opcode helper 缺失注入测试必须覆盖 field_read/field_write/access_context 与 JSON oracle 相等。
- source 归属：头文件 `static inline`、系统头文件 fallback、缺失 `loc.file` fallback、路径逃逸拒绝。
- object_id：不同 `.c` 文件中的 `static helper()` 不冲突；不同 source 中的同名 `static` global 不冲突；同一头文件 inline/global 在多 TU 中稳定；`source_roots` 顺序不影响 id。
- field 行为：field `object_name` 去 type 前缀；同名 field 分属不同 type；头文件声明的同一 owner field 跨 TU 归并；匿名 `struct/union` 和缺失 type fact 场景 materialize field fact；`has_field`、`field_read`、`field_write` 指向正确 field id。
- AST 表达式：链式 `a->b->c`、宏包装 MemberExpr、位运算包裹、ParenExpr/ImplicitCastExpr/CStyleCastExpr 包裹、复合赋值、自增自减、条件、返回值、调用实参和无法稳定判断读写的 partial 字段访问。
- call 行为：`referencedDecl.name` 驱动 direct_call；头文件内联函数跨 TU 归并后 `callers:` 关系查询唯一解析；共享头 type/field/inline/global facts 在 `worker_count>1` 时与串行 snapshot parity，且同 id 多 TU payload 差异由最低 `source_seq` fact 确定性胜出，不触发 `map_reduce_conflict`；宏展开 direct call 统计；field/global/function_pointer_slot 函数指针赋值生成 `assigned_to`；函数指针 callee 生成 `dispatches_via`；`typedef` 包裹的成员函数指针通过 `desugaredQualType` 识别；找不到文件内 callee fact 时记录 bounded unresolved evidence；跨文件 resolver 覆盖 exact source、唯一同名 fallback、linkage-aware fallback、condition 传递、dedupe、external/internal unresolved、ambiguous、missing caller 和 `linkage_filtered_count`，并断言 reducer rebuild 从 facts scalar 列建函数索引、不重建全量 `CodeFact`。
- compile database：`arguments`、`command`、相对 `directory/file`、libclang path-bearing flags 按 entry directory 归一为绝对路径、配置 compile database 时 AST source collection 只抽取 indexed repo source 且 `source_roots` 只做交集过滤、source inventory 保留仓内 included headers 和 reverse include fanout、重复 entry、仓外 entry、malformed JSON、entry 类型错误、缺 `file`、command split 失败。
- per-file flags：allowlist 连写/分写保留，response file、`-Xclang`、plugin/load、codegen、链接、输出类参数及其值被丢弃并计数；libclang parse 中全局 `clang_args` 在前、per-file flags 在后。
- 全量并行：`extractor.worker_count` auto/显式/非法配置，`worker_count=1` 使用单 worker 子进程并保持串行调度语义，大于 1 使用多个 worker 进程，多个 worker 乱序完成时 facts 通过 SQLite-backed reducer、relatives 经过 worker-local exact dedup 后通过 sorted sidecar external merge 按 id 确定序输出 facts/relatives/source inventory，worker segment 与 reducer/merge 使用 storage snapshot-shaped canonical line bytes，duplicate exact/merge/conflict 语义稳定，saturated fallback 不丢未追踪 relative，单文件可恢复错误、timeout 和 worker crash 不取消其他 worker，`extractor.code.worker_pool` 和 `extractor.code.relative_merge` 的字节直通、worker skipped exact、timeout/restart/crash、residual duplicate 和 full-parse 指标进入 log/views。
- 仓内共享头缓存：process-local cache-on/cache-off 的 facts、relatives、source inventory 和 snapshot id 等价；首次 TU 完整物化头内 `static inline` body、field 和函数指针 evidence；后续同进程 TU 命中已发布头声明时跳过子树但仍用 resolver seed 解析当前 TU 的 `direct_call`、`field_read` / `field_write`、`assigned_to` / `dispatches_via`；不同 compile context 或不同 worker 进程不共享；partial AST 只发布无错误恢复子树的头声明；worker 单文件可见 key/seed 是启动时快照；`.h` 作为 source root 时完整遍历。
- partial AST：target libclang parse 固定追加 `-ferror-limit=0`，timeout 基础 120 秒并按文件大小增长；diagnostics severity 为 error/fatal 但 TU 有有效 `TranslationUnitDecl` 时产出 facts、source inventory 和带 `diagnostic_error` / `diagnostic_fatal` / `diagnostic_error_and_fatal` reason 的 `clang_ast_partial` warning；parse failed、空 `inner`、timeout、libclang C API 错误仍为 `clang_ast_failed`，timeout details 暴露 `timeout_seconds`。
- partial AST 精度：含 `RecoveryExpr`、`containsErrors=true`、`isInvalidDecl=true`、未声明类型或未解析 member/call 引用的错误区域时，不得从错误恢复节点或其子树产出错误 fact、relative 或 pending evidence；错误区域外的有效 declarations 仍可抽取。
- source inventory：per-source `compile_command_hash` 配置 compile database 时 AST source 按 per-file entry 计算，未配置 compile database 时按全局 `clang_args` 计算；仓内 included headers 保持 `compile_command_hash=null` 并保留 `included_by`。
- 场景组合：无 compile db、compile db 全命中、compile db 只覆盖仓内 source 子集、空 indexed source set、有 global clang_args、有 header include 和 incremental fanout、多 source_roots、多 profile。
- field_read、field_write、read_write、条件分支和 ambiguous MemberExpr。
- Clang 隐式行号继承、同一行重复 relation 去重、Clang/libclang unavailable、版本不匹配、capability failed、文件级 AST failed/partial warning、source path escape。
- GCC 当前为 AST-only 路径的可选配置；显式无效路径仍需覆盖。

### Storage

- v5 gzip snapshot + read index schema v6 写读、manifest、stats、hash 校验、gzip corruption、digest mismatch；`read_index.sqlite` 必须使用 `fact_k` / `relative_k` / `from_k` / `to_k` 整数代理键投影，并在查询边界映射回公开 fact/relative id。
- 新 snapshot 生成 `.jsonl.gz` 数据文件和 `read_index.sqlite`，不得生成 SQLite sidecar；v4/v3 snapshot、旧 read index schema 不兼容并提示 rebuild；initializer sorted-unique 写入路径不得调用 storage `_prepare_snapshot_staging` 二次 re-sort，且对乱序/重复 id 保持稳定错误；internal pre-encoded sorted-unique path 必须与对象路径产生相同 snapshot id/hash/read index，且不得重建 `StoredFactLine` / `StoredRelativeLine`。
- storage write/open log 包含 `snapshot_format`、`compression`、压缩前后 bytes、分文件 bytes、`read_index_bytes`、`read_index_build_ms`、`read_index_open_ms`、`compression_ratio_percent` 和 `storage_overhead_ratio_percent`。
- init/rebuild 分阶段观测覆盖 `init.stage` 字段稳定性、无 init 记录空状态、`tools/views` 阶段耗时表、CLI init/rebuild 摘要和 `cipher2 status` human/JSON 渲染；`reduce` 用 per-file outcome merge 累计耗时，不按 fact/relative 行计时，`snapshot_write` 阶段 counts 不重复暴露 `bytes_written`。
- multi-term search：empty、single term、multi term、AND 语义、顺序无关、排序稳定、owner-qualified field 消歧。
- relation search：`readers` / `writers` / `accessors` / `callers` / `callees` 谓词、`file:` line-strip、`caller:` / `name:` 同义过滤、ambiguous anchor `needs_refinement`、`matched_endpoint_count > limit` 的 `too_broad`、一跳 `too_broad` 返回 `complete=true` / `budget_exhausted=false`、overlay parity。
- relation search 硬错误：多谓词、空 anchor、`reachable` 格式、`condition:` 和 unsupported `relation_kind` 必须保留下一步动作提示，且不泄漏完整 query、绝对路径或 traceback。
- relation BFS：`callers` / `callees` 默认一跳、`depth:2` 最短 hop 标注、closure 深度上限、`depth:0` / 负数 / 非数字 / 重复 depth / unsupported predicate 的 `needs_refinement`、cycle 去重、root 不作为 endpoint、endpoint filters 不裁剪 frontier、高扇出预算耗尽、slim endpoint rows、base/overlay parity。
- reachability：`reachable:A->B` outgoing 最短 path、direct call 与 dispatch 合成跳、path node 条件序列化、深可达 vs 浅不可达、`complete=true` 不可达和 bounded `complete=false` 区分、预算耗尽时 `budget_exhausted=true` 且无假 `total`。
- FactRelative 读写、field access 统计、relative preview。
- temporary overlay 与 base search 语义一致。
- source tombstone 必须隐藏 dirty source 产生的 base relatives，避免稳定 `object_id` 把已删除 call edge 带回 overlay view。
- active overlay 的 relative preview/count 多次查询必须共享同一 view 级可见 relative 扫描结果，避免 detail 路径重复全量扫描 base relatives。

### MCP

- `tools/list` 只返回 `search` 和 `detail`。
- `tools/call impact` 返回 `unknown_tool`。
- `scope` 参数被拒绝。
- search multi-term 语义、owner-qualified field 消歧、关系型谓词、`depth` parser、传递 closure、`reachable` path 条件、slim relation rows、可执行精化提示和 response budget。
- `isError=true` 的 not_found / relation query hard error 必须给模型可执行下一步，例如重新 `search` 当前 `object_id` 或用 `detail(<fact_id>)` 查看 relative `condition`。
- detail payload、source_context、按 direction + relation_kind 分桶的 relative_preview、最多 8 条顶层扁平兼容样本、序列化 `DetailResponse` 字节上限、endpoint_name / endpoint_profile、call-site rollup、salience 排序、source 多样化、overlay parity、field_read/field_write 展示、高扇入 field_readers/field_writers 截断计数与多样化选择。
- stdio initialize/tools/list/tools/call 生命周期。

### Log / Views

- log schema、redaction、truncation、summary、type-driven toolchain capability 事件、backend、libclang version/library scope、文件级 AST failed/partial warning、CLI JSON warning 清单、CLI setup discovery/MCP config 事件和 write failure 容错。
- term search、field access、field coverage、包装/宏/位运算字段访问、函数指针 dispatch、MCP response budget / relative preview quality、worker pool、worker-local relative dedup、worker timeout/restart/crash、header cache entry/hit/miss/skipped/seed、source fallback、compile database hit/miss/duplicate/ignored/stripped、parse/traverse 耗时、unresolved call、partial AST、parallel direct call resolution 和 missing evidence 字段进入 digest。
- views 只展示 storage/log/incremental section，并从 log 摘要呈现 type-driven capability 状态、backend、libclang version/library scope、worker pool、worker-local relative input/written/skipped/tracked/saturated、worker timeout/restart/crash、header cache entry/hit/miss/skipped/seed、compile database、source fallback、field coverage、包装/宏/位运算字段访问、函数指针 dispatch、MCP response budget / relative preview quality、partial AST、parallel direct call resolution 统计和 `cli.status` / init setup 事件。
- 删除 graph/inference section 后 invalid section 用例。

### Incremental

- dirty detection、debounce、overlay publish、tombstone-only missing source overlay、compile database 场景下 header 变更通过 `included_by` fanout 到依赖 translation unit。
- `content_changed`、`included_header_changed`、`missing`、`compile_command_changed`、`toolchain_changed` dirty reason 构造路径；`toolchain_changed` 和 dirty set 过大进入 base-only stale warning。
- stale/pending/overlay view state；pending/stale 查询返回 base result 并携带 structured content 状态。
- MCP 启动时对 source inventory 与活树做一次同步对账，已保存变更在首个 `search` / `detail` 前体现为 overlay。
- overlay TTL、base snapshot/runtime guard invalidation、warning `overlay_dropped`、`state.json` 优先于 warning log fallback、max_dirty_files。
- incremental performance gate 必须覆盖配置 fake toolchain、`_extractor=None`、active overlay 下重复 query 的生产热路径，断言 toolchain probe 不在每次 query 重跑且 query p95 保持预算内。
- `incremental.worker_count` 只覆盖配置校验与 poll_started configured/active 计数；在线临时增量 v1 不测试真实 worker pool。

### Retrieval Benchmark

- manifest RepoSpec、snapshot lock 校验和 `snapshot_mismatch` skip。
- Clang gold graph 转换为 CALLERS 题池时使用独立 gold answer，不从 cipher 命中结果筛题。
- in-process MCP `search` + `detail` preview 可还原率。
- storage 直读 full 天花板、`bound_loss = recover@full - recover@preview`。
- 高扇入 `FIELD_ACC` 场景中，store full 覆盖但 bounded preview 只召回部分答案时复用既有 `preview_partial` 根因。
- 手动 run 入口输出 `run_summary.json` 和 Markdown 报告。
- retest manifest 校验、`recover@preview` / `recover@full` 聚合、`preview_gap`、`ceiling_delta` 和报告输出。
- 外部弱模型 adapter 的 stdin/stdout JSON 协议、`grep` vs `grep_cipher` 打分、`delta` / `rescue` 和 required env skip 分支。
- #127 T4/T5 类多跳 probe：2 跳 callers/callees closure、reachable yes/no、tool call 数、`complete` / `budget_exhausted` 解读和弱模型避免手动链式 BFS。
- 小型 fixture 进入 unittest；真实 10 库和弱模型 A/B 为人工门禁，不在普通 CI 中运行。

## 覆盖矩阵

覆盖矩阵文件用于保证规格项和测试用例一一对应。新增或删除 public 行为时，必须同步更新对应矩阵：

- `test_cli_coverage_matrix.py`
- `test_config_coverage_matrix.py`
- `test_initializer_coverage_matrix.py`
- `test_storage_coverage_matrix.py`
- `test_mcp_coverage_matrix.py`
- `test_retrieval_benchmark_coverage_matrix.py`
- `test_log_coverage_matrix.py`
- `test_views_coverage_matrix.py`

## 性能门禁

按改动范围运行：

```bash
PYTHONPATH=src python3 scripts/cli_performance_gate.py
PYTHONPATH=src python3 scripts/config_performance_gate.py
PYTHONPATH=src python3 scripts/initializer_performance_gate.py
PYTHONPATH=src python3 scripts/clang_extractor_performance_gate.py
PYTHONPATH=src python3 scripts/storage_performance_gate.py
PYTHONPATH=src python3 scripts/storage_relative_performance_gate.py
PYTHONPATH=src python3 scripts/mcp_performance_gate.py
PYTHONPATH=src python3 scripts/mcp_relative_performance_gate.py
PYTHONPATH=src python3 scripts/incremental_performance_gate.py
PYTHONPATH=src python3 scripts/views_performance_gate.py
```

`scripts/cli_performance_gate.py` 必须覆盖 `cipher2 status` human 和 JSON 渲染路径，按 512MB、4GB、8GB 三档验证耗时和峰值内存。
`scripts/clang_extractor_performance_gate.py` 和 `scripts/initializer_performance_gate.py` 必须覆盖 libclang 进程内遍历、type-driven AST evidence、PG-like 自包含库共享仓内头压力工况、全量 worker 数内存边界、SQLite-backed facts reducer、worker-local relative dedup、relative external merge 确定序输出、field fact 覆盖、field/call 关系、compile database per-file flags、allowlist 参数清洗、source fallback 计数和高 pending 调用密度的 parallel direct call resolver 工况；共享头工况必须报告 TU 数、唯一头声明数、traverse 总时、header cache hit/miss/skipped/seed、relative map input/written/skipped、worker dedup tracked/saturated、worker timeout/restart/crash、relative segment bytes、relative merge wall、accepted/duplicate/conflict、fan-in/pass/peak open segment、`relative_merge_full_parse_count`、read index wall、`init.stage` 各阶段 wall/累计窗口和最终 fact/relative/source inventory 计数。涉及 relation 增量的实现还必须运行 `scripts/storage_relative_performance_gate.py`。`scripts/storage_performance_gate.py` 和 `scripts/storage_relative_performance_gate.py` 必须输出 raw/compressed/total snapshot MB、`read_index_mb`、`read_index_bytes / compressed_data_bytes`、压缩率、storage overhead、`cold_index_open_ms` 和 `first_search_ms`，覆盖 512MB、4GB、8GB 三档内存预算；gzip 数据体积不得高于同一 workload 未压缩 logical bytes 的 55%，总 snapshot 体积不得高于 60%，read index 不得超过 gzip 数据体积的 2 倍。`scripts/log_performance_gate.py` 和 `scripts/views_performance_gate.py` 必须覆盖 backend、libclang version/library scope、parse/traverse 耗时、worker pool、worker-local relative dedup、worker timeout/restart/crash、relative external merge、header cache counts、compile database、parallel direct call resolution、storage compact/read-index snapshot 可观测字段和最近一次 init stage 耗时表。

`graph_*` 和 `inference_*` 测试/脚本已移除；新增测试不得重新依赖 Graph projection、Inference rules 或 MCP `impact`。
