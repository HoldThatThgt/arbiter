# scripts

## 路径职责

本目录存放开发、测试和性能门禁脚本。脚本必须可从仓库根目录运行，并通过 `PYTHONPATH=src` 使用本地源码。

## 基础命令

```bash
git diff --check
PYTHONPATH=src python3 -m unittest discover -s tests
```

## 性能门禁

| 脚本 | 作用 |
|---|---|
| `cli_performance_gate.py` | `cipher2 init/rebuild/status` 包装层性能，覆盖 status human/JSON 渲染。 |
| `config_performance_gate.py` | config load/write 小中大三档。 |
| `initializer_performance_gate.py` | 全量初始化、compile database per-file flags、仓内共享头 cache、direct call resolver、relative external merge、类型驱动 AST 抽取编排和 snapshot 写入。 |
| `clang_extractor_performance_gate.py` | 类型驱动 Clang AST extractor、仓内共享头 cache、compile database index/lookup、field/call evidence、source fallback 和高 pending 调用密度 direct call resolver。 |
| `storage_performance_gate.py` | FACT snapshot、multi-term search、raw/compressed/total 体积、read-index 体积、read-index/压缩数据比例、压缩率、overhead 和冷启动 index 打开耗时。 |
| `storage_relative_performance_gate.py` | FactRelative 读写、preview、relative-heavy 压缩率、read-index 体积、overhead 和冷启动 index 打开耗时。 |
| `mcp_performance_gate.py` | MCP `search` / `detail`。 |
| `mcp_relative_performance_gate.py` | MCP detail relative_preview。 |
| `incremental_performance_gate.py` | 临时 overlay 构建/查询预算，以及配置 toolchain 的 active overlay 查询热路径 guard。 |
| `views_performance_gate.py` | view model 构建，包括 compile database 和 direct call resolution 可观测字段聚合。 |
| `log_performance_gate.py` | JSONL log 写读和 summary，包括 compile database 与 direct call resolution 计数。 |

## 运行示例

```bash
PYTHONPATH=src python3 scripts/storage_performance_gate.py
PYTHONPATH=src python3 scripts/mcp_performance_gate.py
PYTHONPATH=src python3 scripts/clang_extractor_performance_gate.py
```

## 预算

| 场景 | 数据规模 | 预算 |
|---|---|---|
| 小 512MB | 1,000 facts / 1,000 relatives / 1,000 AST evidence，100 search/detail calls | extractor + initializer < 5s，search p95 <= 200ms，peak < 64MB |
| 中 4GB | 100,000 facts / 200,000 relatives | extractor + initializer < 120s，search p95 <= 200ms，detail p95 <= 50ms，peak < 512MB |
| 大 8GB | 1,000,000 facts / 2,000,000 relatives | extractor + initializer < 1,200s，search p95 <= 500ms，detail p95 <= 100ms，peak < 2GB |

性能脚本必须输出 JSON 摘要，失败时抛出 AssertionError 或返回非零退出码。`clang_extractor_performance_gate.py` 和 `initializer_performance_gate.py` 的共享头工况必须报告 TU 数、唯一头声明数、traverse 总时、header cache hit/miss/skipped/seed、relative map input/written/skipped、worker dedup tracked/saturated、relative segment bytes、relative merge wall、relative accepted/duplicate/conflict、fan-in/pass/peak open segment、`relative_merge_full_parse_count`、read index wall 和最终 fact/relative/source inventory 计数，用于确认 traverse 成本接近唯一头声明规模、重复 relative 在 worker 写段前被源头过滤、relative merge 未回退到主进程 full payload parse，且 read index 耗时没有混入 merge 口径。
