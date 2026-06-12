# Schema 说明

本文说明 `cipher-2` v1 的逻辑 schema 和目标 snapshot 文件。当前运行时只承诺 FACT、FactRelative 和 source inventory；Graph projection 和 Inference rule framework 已退出主线。

## Snapshot Layout

```text
.cipher/
  snapshots/
    current
    <snapshot_id>/
      facts.jsonl.gz
      relatives.jsonl.gz
      source_inventory.jsonl.gz
      read_index.sqlite
      manifest.json
      stats.json
```

snapshot schema v5 使用 `compact-jsonl-gzip` 格式：三个数据文件都是 gzip 压缩后的 canonical JSONL，压缩参数固定为 `gzip-1`。manifest 中的 `facts_sha256`、`relatives_sha256` 和 `source_inventory_sha256` 表示未压缩 line stream 的 hash，并继续作为 snapshot identity 输入；`read_index.sqlite` 是同一 snapshot 的派生只读 SQLite 索引，不参与 identity，但 v5 snapshot 必须存在该文件。`compressed_data_bytes` 表示三个 gzip 数据文件大小，`bytes_on_disk` 表示数据文件、read index、manifest 和 stats 总大小，`uncompressed_bytes` 和 `file_bytes` 表示压缩前体积和分文件压缩统计。v4/v3 snapshot 不做回读兼容，需通过 `cipher2 rebuild` 重建。

`read_index.sqlite` 使用独立 schema version `6`，保存 `index_metadata`、`facts`、`fact_keys`、`relative_ids` 和 `relatives` 表。`index_metadata` 必须与 manifest 的 snapshot id、hash 和 counts 对齐，`projection_kind` 固定为 `proxy-key-column-projection`；`facts` 继续以公开 `object_id TEXT PRIMARY KEY` 保存返回所需固定字段、`payload_json`、`fact_kind_rank` 和稀疏 Unicode casefold fallback 列；`fact_keys` 保存 `fact_k -> object_id` 映射，并只在 snapshot 含 relatives 时写入行；`relative_ids` 保存 `relative_k -> relative_id` 映射；`relatives` 只保存 `from_k`、`to_k`、`relative_k`、整数 `relation_kind_code`、条件/payload JSON 以及整数 endpoint 索引，读出有限结果时再通过 `fact_keys` / `relative_ids` 映射回对外 fact/relative id 字符串。`fact_k` 按 `object_id` 升序分配，`relative_k` 按 `relative_id` 升序分配，因此对外排序语义不变。搜索快路径使用 SQLite `lower()/instr()`，fallback 列保持完整 Unicode casefold 语义；`fact_kind_rank` 保证同名 type/function/global 等定义类 fact 不被 field fact 淹没。SQLite 使用 1024 byte page 和 `WITHOUT ROWID` 主键表看护小型 snapshot 体积。缺失、损坏、schema version 5 旧索引或 metadata mismatch 时，查询必须稳定报错并提示 rebuild，不得静默重建内存索引。

旧 snapshot 中如果存在 `graph_objects.jsonl`、`graph_relatives.jsonl` 或 `graph_derived_from.jsonl`，读路径必须忽略，不得作为 public contract。

## TheFact

`TheFact` 表示一个可被搜索和 detail 展开的代码事实。

| 字段 | type | 说明 |
|---|---|---|
| `object_id` | `str` | 稳定 fact id；C extractor 对 function/type/field 使用 source 或 owner identity 区分同名符号。 |
| `object_name` | `str` | 人类可读名称；field 只保存字段名，不包含 type 前缀。 |
| `object_description` | `str` | 短描述。 |
| `object_source` | `str` | 仓库相对源码位置；C extractor 优先使用 AST `loc.file` / `range.begin.file`，相对路径按当前 translation unit 的 compile command `directory` 解析，未命中 compile database 时按该 TU 的源码父目录解析；缺失、系统路径或路径逃逸时回退到当前 translation unit。 |
| `object_profile` | `str` | profile，例如 `default`、`debug`。 |
| `object_caller` | `str or null` | 调用方摘要，主要用于关系型 facts。 |
| `object_callee` | `str or null` | 被调方摘要，主要用于关系型 facts。 |
| `payload` | `dict` | 有界结构化字段，不保存源码正文。 |

当前 C extractor 的主要 `fact_kind`：

- `code_file`
- `function`
- `global`
- `type`
- `field`
- `macro`
- `function_pointer_slot`
- `diagnostic`

C extractor 的 identity 规则：

| fact_kind | identity 输入 | 说明 |
|---|---|---|
| `function` | `symbol_name`、`canonical_source`、`linkage` | 区分不同 `.c` 文件中的同名 `static` 函数；头文件内联定义按头文件 source 归并。 |
| `global` | `symbol_name`、`canonical_source`、`linkage` | 头文件 `extern` global 跨 translation unit 归并；不同 source 中的同名 `static` global 不冲突。 |
| `type` | `symbol_name`、`canonical_source` | 区分不同 source 中的同名类型。 |
| `field` | `owner_name`、`symbol_name`、`canonical_source` | 同名字段可产生多个 fact；owner 不写入 `object_name`；匿名 owner 使用稳定 synthetic identity。 |
| `function_pointer_slot` | `slot_name`、`canonical_source`、owner function、line/column | 区分不同函数中的同名本地函数指针变量。 |

头文件 `static inline` / `always_inline` 函数即使被多个 translation unit 消费，也必须以头文件声明 source 形成一个 canonical function fact。头文件 `extern` global 即使被多个 translation unit 消费，也必须以 `(symbol_name, canonical_source, linkage)` 形成一个 canonical global fact；`payload.ordinal`、`payload.source_id` 和物化它的 translation unit 不得参与 global identity。field fact 的 owner 通过 `payload.owner_name`、`payload.owner_type_id`、`payload.canonical_source` 和 incoming `has_field` 表达。查询展示不得依赖 `Type.field` 字符串。C extractor 必须为每个有非空字段名的 `FieldDecl/IndirectFieldDecl` 创建或复用 field fact；匿名 `struct/union` 和 type fact 解析失败时必须创建 synthetic type owner，保证 `MemberExpr.referencedMemberDecl` id 有唯一 field fact 可指向。

## FactRelative

`FactRelative` 表示 FACT 之间的结构化关系。它是模型理解跨接口行为的主要证据层。

| 字段 | type | 说明 |
|---|---|---|
| `relative_id` | `str` | 稳定 relation id。 |
| `from_fact_id` | `str` | 起点 fact id。 |
| `to_fact_id` | `str` | 终点 fact id。 |
| `relation_kind` | `str` | 关系类型。 |
| `condition` | `RelativeCondition or null` | 保守条件摘要。 |
| `confidence` | `float` | 抽取置信度。 |
| `object_profile` | `str` | profile。 |
| `payload` | `dict` | 有界 evidence 字段。 |

主要 `relation_kind`：

| relation_kind | 方向 | 说明 |
|---|---|---|
| `include` | `code_file -> code_file` | 文件 include。 |
| `defines` | `code_file -> fact` | 文件定义函数、类型、字段、宏等。 |
| `declares` | `code_file -> fact` | 文件声明对象。 |
| `has_field` | `type -> field` | 类型包含字段，是 field owner 的权威关系。 |
| `direct_call` | `function -> function` | 直接函数调用，只能来自 Clang call reference；跨文件调用由 extractor 后处理补齐。 |
| `assigned_to` | `function_pointer_slot/global/field -> function` | 函数指针或槽位赋值。 |
| `dispatches_via` | `function -> function_pointer_slot/global/field` | 通过函数指针或槽位间接调用。 |
| `field_read` | `function -> field` | 函数读取字段。 |
| `field_write` | `function -> field` | 函数写入字段。 |

`field_read` / `field_write` 必须来自 Clang AST `MemberExpr` 的 member reference 和类型 evidence，`MemberExpr` 可位于宏展开、位运算、cast、括号、条件、实参或返回表达式的包装子树中；未能解析到唯一 field identity 时不得生成模糊边。跨文件 callee 尚未解析时，只允许记录有界 unresolved evidence；所有文件映射完成后，direct call resolver 先按 `(callee_name, referenced_source)` 精确匹配，再做 linkage-aware 唯一同名 fallback。其他 source 的 `static` / `internal` 候选必须过滤；过滤后不唯一或无候选时不得生成猜测 `direct_call`。

`assigned_to` / `dispatches_via` 必须来自 Clang AST 的函数指针类型、声明引用和唯一 endpoint：struct field 使用 `field` fact，文件级函数指针变量使用 `global` fact，本地函数指针变量使用 `function_pointer_slot` fact。函数指针类型识别必须同时使用 `qualType` 和 `desugaredQualType`，覆盖 `typedef` 包裹的成员函数指针。`CallExpr` callee 为 `MemberExpr` / `DeclRefExpr` 且 endpoint 唯一时生成 `dispatches_via`；解析不到唯一 endpoint 或 target function 时不得生成猜测边。查询层的 call closure 可把同一 endpoint 上的 `dispatches_via` + `assigned_to` 合成为函数到函数的候选跳，但 snapshot 中仍保留原始两类 relation。

## Source Inventory

source inventory 描述 snapshot 输入文件和增量判断所需元数据。

| 字段 | type | 说明 |
|---|---|---|
| `source_id` | `str` | 稳定 source id。 |
| `rel_path` | `str` | 仓库相对路径。 |
| `source_kind` | `str` | 文件类型。 |
| `sha256` | `str` | 文件内容 hash。 |
| `size_bytes` | `int` | 文件大小。 |
| `mtime_ns` | `int` | 修改时间。 |
| `compile_command_hash` | `str or null` | 当前 source 的 compile command 摘要；配置 compile database 的独立 AST source 来自 per-file entry；未配置 compile database 时来自全局 `clang_args`；被 include graph 跟踪的 header 和非 C source 可为空。 |
| `toolchain_hash` | `str or null` | Clang capability、GCC 配置输入和 profile 摘要。 |
| `included_by` | `list[str]` | 反向 include source ids。 |
| `includes` | `list[str]` | 正向 include source ids。 |

本次 per-file compile database 支持不改变 source inventory schema。它只改变 `compile_command_hash` 的取值来源，使在线临时增量可以发现单个 source 的编译参数变化。

## 查询语义

`search` 只读 FACT view。非空 query 使用 whitespace 分词，所有 term 必须命中同一 fact 的可搜索字段。排序按加权分数降序、`object_id` 升序。

`detail` 只接受 FACT id，返回 payload、source context 和 relative preview。relative preview 是 bounded 摘要，不替代 storage 内部审计接口。
