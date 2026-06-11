# storage/schema

## 路径职责

本目录记录 storage snapshot 的文件级 schema。当前 runtime 只承诺 FACT、FactRelative、source inventory、manifest 和 stats；Graph projection 文件属于旧产物，读路径忽略，不作为 schema。

## Snapshot 文件

```text
.cipher/snapshots/<snapshot_id>/
  facts.jsonl.gz
  relatives.jsonl.gz
  source_inventory.jsonl.gz
  read_index.sqlite
  manifest.json
  stats.json
```

| 文件 | 内容 | 必须存在 |
|---|---|---|
| `facts.jsonl.gz` | gzip 压缩后的 `FactRecord` JSONL 行 | 是 |
| `relatives.jsonl.gz` | gzip 压缩后的 `FactRelative` JSONL 行 | 是 |
| `source_inventory.jsonl.gz` | gzip 压缩后的 `SourceInventoryEntry` JSONL 行 | 是 |
| `read_index.sqlite` | FACT search/detail/relative preview 使用的只读 SQLite 整数代理键投影索引 | 是 |
| `manifest.json` | snapshot metadata、counts、hash、格式和压缩统计 | 是 |
| `stats.json` | status/views 使用的 storage stats | 是 |

旧文件 `graph_objects.jsonl`、`graph_relatives.jsonl`、`graph_derived_from.jsonl` 如果存在，必须被忽略。

数据文件使用标准库 `gzip`，固定 `compresslevel=1`、`mtime=0`。gzip 内部仍是逐行 canonical JSON。`facts_sha256`、`relatives_sha256` 和 `source_inventory_sha256` 均表示未压缩 canonical line stream 的 SHA-256，不表示 gzip 字节的 hash。

## JSONL 行格式

压缩前每行都是 canonical JSON。`facts` 和 `relatives` 行包含 top-level identity 字段、`payload` 和 `payload_sha256`：

```json
{"schema_version":5,"object_id":"...","fact_kind":"function","payload":{},"payload_sha256":"..."}
```

`payload_sha256` 是 canonical payload JSON 的 hash。读取时必须校验 schema version、payload 类型、payload hash、gzip CRC 和未压缩 line stream hash。

`source_inventory` 行直接展开 `SourceInventoryEntry` 字段，并附加 `schema_version` 与 `payload_sha256`；其 `payload_sha256` 基于去掉这两个 envelope 字段后的 canonical JSON。

## FactRecord Payload

| 字段 | type | 说明 |
|---|---|---|
| `object_id` | `str` | 稳定 id；C function/type/field 可包含 source 或 owner identity。 |
| `object_name` | `str` | 可搜索名称；C field 只保存字段名，不含 type 前缀。 |
| `object_description` | `str` | 可搜索描述。 |
| `object_source` | `str` | 仓库相对源码位置。 |
| `object_profile` | `str` | profile。 |
| `object_caller` | `str or null` | 可搜索 caller。 |
| `object_callee` | `str or null` | 可搜索 callee。 |
| `payload` | `dict` | 有界结构化字段。 |

## FactRelative Payload

| 字段 | type | 说明 |
|---|---|---|
| `relative_id` | `str` | 稳定 relation id。 |
| `from_fact_id` | `str` | 起点 fact id。 |
| `to_fact_id` | `str` | 终点 fact id。 |
| `relation_kind` | `str` | 关系类型。 |
| `condition` | `object or null` | 条件摘要。 |
| `confidence` | `float` | 置信度。 |
| `object_profile` | `str` | profile。 |
| `payload` | `dict` | 有界 evidence。 |

新增字段访问关系：

| relation_kind | 方向 | 说明 |
|---|---|---|
| `field_read` | `function -> field` | 函数读取字段。 |
| `field_write` | `function -> field` | 函数写入字段。 |

`direct_call` 只能来自 Clang call reference。文件内 callee 可由 mapper 直接写入；跨文件 callee 只能由 extractor 在所有文件映射完成后消费 `DirectCallEvidence` 补齐。后处理必须优先 `(callee_name, referenced_source)` 精确匹配，fallback 只能使用 linkage-aware 的唯一同名 function fact；其他 source 的 `static` / `internal` function 不得被跨 translation unit 连边。无法唯一解析时不写 `FactRelative`。

`assigned_to` 使用 `function_pointer_slot/global/field -> function`；`dispatches_via` 使用 `function -> function_pointer_slot/global/field`。storage 只接受 extractor 给出的唯一 endpoint，不根据名称重建函数指针调用图。本地函数指针变量必须写为 `function_pointer_slot` fact，object identity 由 slot 名称、source、owner function 和 line/column 构成。查询层的 callers/callees/reachable call closure 可以把同一 slot 上的 `dispatches_via` 和 `assigned_to` 合成为函数到函数的候选跳；该合成只用于查询结果和 path 标注，snapshot 中仍保留原始两类 relation。

`has_field` 是 field owner 的权威关系；`field_read` / `field_write` 必须指向唯一 field fact，可来自宏展开、位运算或 cast/括号等包装表达式内的 Clang `MemberExpr`。storage 只持久化 extractor 给出的 identity，不重新推导 `Type.field`。匿名 `struct/union` 或缺失 type fact 时，extractor 必须提供 synthetic owner/type fact，使每个有名字的 `FieldDecl/IndirectFieldDecl` 都能被写成 field fact。

## SourceInventoryEntry Payload

| 字段 | type | 说明 |
|---|---|---|
| `source_id` | `str` | 稳定 source id。 |
| `rel_path` | `str` | 仓库相对路径。 |
| `source_kind` | `str` | 文件类型。 |
| `sha256` | `str` | 文件内容 hash。 |
| `size_bytes` | `int` | 文件大小。 |
| `mtime_ns` | `int` | 修改时间。 |
| `compile_command_hash` | `str or null` | 当前 source 的 compile command 摘要；配置 compile database 的独立 AST source 来自 per-file entry，未配置 compile database 时来自全局 `clang_args`；被 include graph 跟踪的 header 可为空。 |
| `toolchain_hash` | `str or null` | toolchain 和 profile 摘要。 |
| `included_by` | `list[str]` | 反向 include source ids。 |
| `includes` | `list[str]` | 正向 include source ids。 |

## Manifest

manifest 至少包含：

- `snapshot_id`
- `snapshot_format`
- `compression`
- `created_at`
- `fact_count`
- `relative_count`
- `source_count`
- `facts_sha256`
- `relatives_sha256`
- `source_inventory_sha256`
- `bytes_on_disk`
- `uncompressed_bytes`
- `compressed_data_bytes`
- `compression_ratio`
- `storage_overhead_ratio`
- `file_bytes`
- `read_index`
- `schema_version`

`snapshot_format` 固定为 `compact-jsonl-gzip`，`compression` 固定为 `gzip-1`。`compressed_data_bytes` 统计三个 gzip 数据文件总大小；`bytes_on_disk` 统计数据文件、`read_index.sqlite`、manifest 和 stats 的实际 snapshot 大小；`uncompressed_bytes` 统计三个数据文件压缩前总字节数；`compression_ratio` 按 `compressed_data_bytes / uncompressed_bytes` 保留两位小数；`storage_overhead_ratio` 按 `bytes_on_disk / uncompressed_bytes` 保留两位小数；`file_bytes` 分别记录 `facts`、`relatives`、`source_inventory` 的 raw/compressed 字节数。`read_index` 至少包含 `file_name`、`index_format`、`schema_version`、`projection_kind`、`payload_codec`、`bytes_on_disk`、`fact_count` 和 `relative_count`。不再写 Graph 文件 hash 或 Graph stats。旧 manifest 中的 Graph 字段应被忽略。

## Read Index

`read_index.sqlite` 使用 schema version `6`。它是 gzip JSONL 的派生物，不参与 `snapshot_id` identity。SQLite 使用 1024 byte page，主键表使用 `WITHOUT ROWID`，并禁用 WAL；snapshot 中不得出现 `read_index.sqlite-wal`、`read_index.sqlite-shm` 或 `read_index.sqlite-journal`。旧 schema version `5` 的 read index 不回读，必须通过 `cipher2 rebuild` 重新生成。

必需表：

- `index_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)`：保存 `snapshot_id`、三个 logical sha256、counts、`projection_kind=proxy-key-column-projection` 和 `payload_codec=json-text`。
- `facts`：以公开 `object_id TEXT PRIMARY KEY` 保存 fact 固定字段、`payload_json`、`fact_kind_rank` 和稀疏 `object_*_cf` fallback 列，继续服务 search/detail 和空 query 的 `object_id ASC` 排序。
- `fact_keys`：以 `fact_k INTEGER PRIMARY KEY` 保存 `fact_k -> object_id` 映射，并建立 `object_id` 唯一索引用于 relation 查询边界解析。`fact_k` 按 canonical `object_id` 升序从 1 分配；当 snapshot 没有 relatives 时该表只保留 schema、不写入行，避免 fact-only read index 体积回退。
- `relative_ids`：以 `relative_k INTEGER PRIMARY KEY` 保存公开 `relative_id`。不为 `relative_id` 建唯一索引；唯一性由已排序的 `relatives.jsonl.gz` stream 和 snapshot 写入路径保证。
- `relatives`：以 `relative_k INTEGER PRIMARY KEY` 保存 `from_k`、`to_k`、`relation_kind_code`、confidence、profile/evidence、`condition_json` 和 `payload_json`，并建立 `(from_k, relation_kind_code)` 与 `(to_k, relation_kind_code)` 索引。`relative_k` 按 canonical `relative_id` 升序从 1 分配，读出有限结果时 join `relative_ids`、`fact_keys` 和 `facts` 映射回公开字符串。`relation_kind_code` 使用按 relation kind 字符串排序后的稳定整数映射；`object_profile="default"` 在 index 中编码为 `NULL`，读出时还原为公开字符串。

搜索快路径使用 SQLite `lower()/instr()`；稀疏 fallback 列覆盖 `ß -> ss`、非 ASCII 大写等 SQLite ASCII `lower()` 无法表达的 Unicode casefold 场景，避免把所有 casefold 文本重复写入磁盘。

## 兼容策略

- 缺失必要 `.jsonl.gz` 数据文件时 snapshot corrupt。
- schema v5 不兼容 v4/v3 snapshot；用户必须执行 `cipher2 rebuild` 重建。
- 缺失、损坏或 metadata mismatch 的 `read_index.sqlite` 必须返回稳定错误并提示 rebuild，不得静默从 gzip 重建内存索引。
- gzip 解压失败、CRC 失败或 logical digest mismatch 时 snapshot corrupt。
- 旧 Graph 文件存在时不报错。
- 旧 manifest Graph 字段存在时不报错。
- 新 writer 不再生成 Graph 文件。
