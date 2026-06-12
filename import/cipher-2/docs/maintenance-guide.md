# 维护手册

## 架构边界

`cipher-2` v1 是 FACT-only 运行时：

- 类型驱动 Clang AST 抽取 `TheFact`、`FactRelative` 和 source inventory。
- Clang 准入以 `loc.file`、call reference、member reference 和 `qualType` evidence capability probe 为准；当前 AST-only 路径不要求 GCC 存在。
- storage 写入 `.cipher/snapshots/<snapshot_id>/`，数据文件使用 v5 `compact-jsonl-gzip`，并同步生成持久 `read_index.sqlite`；当前 read index schema v6 使用整数代理键投影压缩 relatives endpoint 和 relative id。
- MCP 只公开 `search` 和 `detail`。
- 在线临时增量只发布可丢弃 overlay，不移动 `snapshots/current`；查询热路径只做 TTL、`snapshots/current` 指针和 cached toolchain fingerprint 的轻量 guard，完整 storage/read-index schema、dirty source compile command 和 source inventory toolchain 校验放在启动对账、notify 与轮询扫描中，pending/stale 查询仍返回 base view。
- `tools/log` 是机器事件源，`tools/views` 是人类可读状态入口。

不保留 Graph projection、Inference rule framework、MCP `impact`、Graph scope、`.cipher/inference/` 或交互式 inference setup。旧配置和旧 snapshot 中相关 Graph/Inference 字段必须被忽略并给出兼容警告；v4/v3 snapshot 不做回读兼容，需通过 `cipher2 rebuild` 重建为 v5 gzip + read index。

## 开发流程

所有文档使用中文。功能开发和改变规格的 bug 修复必须遵守：

1. 设计草稿写入 `docs/design-drafts/YYYYMMDD-topic.md` 并提设计 PR。
2. 设计 PR 合入后，将草稿搬迁到对应模块 README，并递归更新到顶层文档。
3. README 搬迁 PR 合入后，严格按 TDD 实现代码。
4. 实现 PR 无需再向用户确认，但必须全量用例通过；PR 合入视为通过。

开发前设计必须包括模块定位、规格约束、数据结构、对外接口、并发控制、可观测性和测试门禁。数据结构和接口流程使用 Mermaid；class 成员使用表格，至少包含成员名称、type、作用和并发粒度。规格和约束必须明确是否新增用户可配配置项。

## 阶段约束

- 设计 PR 只能修改 `docs/design-drafts/`、草稿索引和必要的流程状态文档，不得写运行时代码、测试或模块 README 规格。
- README 搬迁 PR 只能把已合入设计搬迁到模块 README、用户文档、维护文档和顶层 README，不得写运行时代码或测试。
- 实现 PR 必须以 README 中的权威规格为准，先补失败测试，再写最小实现。
- 已完成事项必须及时关闭对应 issue，并移除维护文档中的临时阶段说明，避免把过期状态留在仓库内。

## 测试门禁

基础门禁：

```bash
git diff --check
PYTHONPATH=src python3 -m unittest tests.test_live_libclang_smoke
PYTHONPATH=src python3 -m unittest discover -s tests
```

仓库 CI 位于 `.github/workflows/ci.yml`：

- PR 和 `main` push 自动运行 `git diff --check`、全量 `py_compile`、CLI smoke test、toolchain-gated live libclang smoke 和全量 unittest。live smoke 本机缺 Clang/libclang capability 时 skip；CI 设置 `CIPHER2_REQUIRE_LIVE_LIBCLANG=1`，避免生产 ctypes backend 覆盖退回静默 skip。
- `workflow_dispatch` 支持手动选择 `run_performance=true`，运行全部性能门禁脚本。
- CI 不替代本地按改动范围运行性能门禁；实现 PR 的说明仍必须列出本地已运行命令。

按改动范围追加：

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

涉及 MCP 检索呈现、关系抽取覆盖或 `search` / `detail` 质量的变更，还应手动运行检索质量 harness。该 harness 不属于默认 CI 硬门禁，只消费已经构建并锁定的目标仓库 `.cipher/snapshots/current`：

```bash
PYTHONPATH=src:. python3 -m benchmarks.retrieval.run --manifest /path/to/retrieval-manifest.json --output /tmp/cipher2-retrieval-report
```

报告中的 `recover@preview` 来自 in-process MCP `search` + `detail`，`recover@full` 来自 storage 直读天花板，`bound_loss` 表示二者差距。

新功能必须达到功能点覆盖率 100%、异常分支覆盖率 90%+、场景用例覆盖率 100%。性能和小型化看护覆盖 512MB、4GB、8GB 三档。

检索质量复测使用 `benchmarks/retrieval/`，属于人工触发的开发回归基准，不进入普通 CI 硬门禁。实现影响 MCP 呈现、关系覆盖或检索排序时，维护者应准备目标库 snapshot 后运行：

```bash
PYTHONPATH=src:. python3 -m benchmarks.retrieval.run --manifest /path/to/retest-manifest.json --mode probe --output /tmp/cipher2-retest
```

弱模型 A/B 只通过 manifest 中的 `external_command` adapter 启用；没有模型环境时必须在报告中记录 `skipped`，不得伪造 `acc_B`、`acc_C`、`delta` 或 `rescue`。

涉及跨文件 `direct_call` 后处理时，性能门禁必须包含高 pending 调用密度工况，并确认 resolver 只持有已有 function fact 引用，不复制 payload、不保留 AST、不重新读取源码。

涉及全量 init/rebuild 抽取并行度时，测试必须覆盖 `extractor.worker_count=1` 单 worker 子进程串行等价、大于 1 使用多个 worker 进程、worker 只写 run-local map 段、worker-local relative exact 去重与 saturated fallback、乱序完成后通过 SQLite-backed facts reducer 和 relatives external merge 按 id 确定序输出、storage sorted-unique 路径不二次 re-sort、单文件可恢复错误/timeout/worker crash 隔离、parallel direct_call resolver 可观测字段、`extractor.code.worker_pool` 的 map segment/bytes、relative input/written/skipped/tracked/saturated、timeout/restart/crash 与 stale run GC 字段，以及峰值内存按 `worker_count * 单文件窗口 + 函数索引 + SQLite cache + relative dedup cap` 有界而不是随全仓 facts/relatives 增长。

涉及 `cipher2 init` setup UX 时，测试必须覆盖 compile database 自动探测优先级、未发现时的可操作 warning、只写 `paths.compile_database` 不解析内容、build readiness 预检事件、toolchain report-only、不写 toolchain config、repo-root `.mcp.json` 新建/merge/幂等/malformed 不覆盖、`--no-mcp-config`、`--print-mcp-config`、CLI JSON setup object、log/views 可观测字段，以及不支持仓库外客户端配置写入 flag。

涉及 C extractor AST backend 性能时，测试和性能门禁必须确认正式路径使用标准库 `ctypes` 调用 libclang C API 进程内遍历，不生成完整 TU JSON、不调用 `-ast-dump=json` fallback、不引入 PyPI 运行时依赖；同时覆盖 libclang 自动定位、last-resort `extractor.code.libclang_library`、Clang/libclang 版本匹配、diagnostics severity 到 `diagnostic_reason` 的映射，以及 per-file parse/traverse 可观测字段。

涉及 storage compact snapshot 或持久 read index 时，`scripts/storage_performance_gate.py` 和 `scripts/storage_relative_performance_gate.py` 必须输出 raw/compressed/total snapshot MB、`read_index_mb`、`read_index_bytes / compressed_data_bytes`、压缩率、storage overhead、`cold_index_open_ms` 和 `first_search_ms`；large 工况必须确认内存仍覆盖 512MB、4GB、8GB 三档，gzip 数据体积不高于同一 workload 未压缩 logical bytes 的 55%，总 snapshot 体积不高于 60%，且 read index 不超过 gzip 数据体积的 2 倍。涉及 read index schema 变更时，还必须覆盖旧 schema mismatch 提示 rebuild、search/relation/detail 结果逐条等价和排序键对外语义不变。

## 发布前检查

- `README.md`、`docs/`、模块 README、`tests/README.md` 与实现一致。
- MCP `tools/list` 只包含 `search` 和 `detail`。
- 新 snapshot 使用 `facts.jsonl.gz`、`relatives.jsonl.gz`、`source_inventory.jsonl.gz` 和 `read_index.sqlite`，不写 Graph 文件或 SQLite sidecar。
- 旧 config 和旧 Graph snapshot 兼容路径有测试覆盖；v4/v3 snapshot 不兼容路径、旧 read index schema、read index 缺失/损坏路径有 rebuild 提示和错误测试覆盖。
- 日志和 views 不泄漏源码正文、绝对 target path、完整 query、traceback、secret 或 provider internals。
