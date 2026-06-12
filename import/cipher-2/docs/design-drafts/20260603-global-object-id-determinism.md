# global object_id 确定性修复设计草稿

- 状态：设计草稿；未搬迁到 README；未实现。
- 关联 issue：#196。
- 范围：修复 C extractor 中 `global` fact 的 `object_id` 输入，使同一仓库、同一输入、任意 worker 完成顺序和 header cache 命中状态下生成等价 snapshot。

## 模块定位

- `src/cipher2/initializer/extractor/code/`：调整 `global` fact 的 identity 构造规则；保留现有 Clang AST-only 路径，不增加字符串解析或跨 collect 全局缓存。
- `docs/schema.md` 与 `src/cipher2/initializer/extractor/code/README.md`：设计合入后搬迁权威规格，补上 `global` identity 行。
- `tests/`：实现阶段新增回归，覆盖同一头文件 `extern` global 被不同 translation unit 先物化时仍归并为同一 `object_id`，并覆盖同输入重复 init 的 snapshot id 稳定性。
- `storage`、`mcp`、`tools/log`、`tools/views`：不改 schema 和对外接口，只消费新的稳定 `object_id`。

## 规格约束

`global` identity 必须与 function/type/field 一样只使用符号语义输入：

| fact_kind | object_id 输入 | 不得参与 identity |
|---|---|---|
| `global` | `symbol_name`、`canonical_source`、`linkage` | `ordinal`、`source_id`、当前 translation unit、worker 完成顺序、header cache 命中状态 |

约束：

- `object_id` 构造仍是纯函数，不依赖遍历顺序、`source_roots` 顺序或最后处理的 translation unit。
- `payload.source_id`、`payload.ordinal`、`payload.line` 可继续作为诊断和 reducer payload 胜出证据保留，但不得进入 `global` identity。
- 头文件中的同一命名 `extern` global 被多个 `.c` translation unit 消费时，必须归并为一个 global fact；不同 `.c` 文件中的同名 `static` global 仍由不同 `canonical_source` 区分。
- 当前 mapper 只为有非空 `name` 的 top-level `VarDecl` 生成 `global` fact；不存在需要靠 `ordinal` 兜底的匿名 global。未来若支持无名全局对象，必须单独设计稳定 synthetic identity，不得回退到遍历序号。
- 本修复改变 global `object_id`，无需兼容旧 snapshot；用户通过 `rebuild` 生成新 v5 snapshot。

## 数据结构

- 在 `_object_identity_payload()` 中为 `fact_kind == "global"` 增加显式分支，写入 `canonical_source` 与 `linkage`。
- `CodeFact`、`FactRelative`、snapshot v5 文件格式、read index 表结构和 MCP response shape 均不变。
- reducer 的同 `object_id` payload 胜出规则不变：同 id 多 TU payload 差异仍按最小 `source_seq` 选择完整 payload，保证输出确定。

## 对外接口

不新增 CLI 参数、配置项、MCP tool、日志字段或 view model 字段。用户可见变化仅为重新 init/rebuild 后 global fact 的 `object_id` 与 snapshot id 变为确定；既有查询语义不扩大、不降级。

## 并发控制

ProcessPoolExecutor 仍允许 worker 乱序完成，coordinator 仍按 `object_id`、`relative_id` 和 `source_id` 确定序输出。由于 `global` identity 不再包含 per-TU / per-traversal 输入，同一 header global 即使由不同 worker 或不同 TU 首次物化，也会在 reducer 阶段按同一 id 去重。

## 递归文档更新

设计 PR 合入后，README 搬迁 PR 更新：

- `docs/schema.md`：identity 表新增 `global` 行，并说明 `ordinal` / `source_id` 不参与 global identity。
- `src/cipher2/initializer/extractor/code/README.md`：object_id 输入表新增 `global` 行，并补充 header extern global 跨 TU 归并约束。
- `tests/README.md`：object_id 与全量并行范围补充 global determinism 覆盖。

README 搬迁 PR 合入前不得修改运行时代码或测试。

## 可观测性

不新增运行时观测字段。实现阶段用现有 artifact 观测确定性：

- 两次同输入 `init` 的 `snapshot_id`、fact count、relative count 必须一致。
- `extractor.code.worker_pool` 仍呈现 worker_count、segment count 和成功文件数，用于确认并行路径被覆盖。
- 若 global 同 id payload 存在 per-TU 差异，仍由现有 reducer 胜出规则保留最小 `source_seq` payload，避免因 payload 字段差异产生非确定输出。

## 可观测用例看护

实现阶段新增或扩展以下用例：

- synthetic AST：`include/hooks.h` 声明 `extern int (*get_attavgwidth_hook)(void);`，`src/a.c` 与 `src/b.c` 均 include；两种 source 顺序或两次 collect 中，global fact 数量为 1，`object_id` 相同，payload 的 `ordinal` 差异不影响 id。
- parallel path：`worker_count=2` 与 `worker_count=1` 对同 fixture 的 facts/relatives/source inventory signature 等价。
- snapshot path：同一临时仓库连续两次 `init` 或等价初始化写 snapshot，`snapshot_id` 完全一致。
- static collision guard：不同 source 中同名 `static` global 仍产生不同 `object_id`。
- unnamed guard：无非空 `name` 的 top-level `VarDecl` 不产生 global fact；如未来改变该行为，必须先补稳定 synthetic identity 测试。

## 测试门禁

实现 PR 至少运行：

```bash
PYTHONPATH=src python3 -m unittest tests.test_code_extractor_fixtures tests.test_code_extractor_parallel tests.test_initializer_api
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src python3 scripts/initializer_performance_gate.py
PYTHONPATH=src python3 scripts/clang_extractor_performance_gate.py
```

本设计阶段不运行实现门禁，不修改代码。
