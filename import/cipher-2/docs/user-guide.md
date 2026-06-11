# 用户手册

## 安装

```bash
python3 -m pip install -e .
cipher2 --version
```

分析 C 仓库需要可执行 Clang 及同一工具链的 libclang，并且二者必须通过类型驱动 AST capability probe。Python 侧只用标准库 `ctypes` 调用 libclang C API，不需要额外 PyPI 运行时依赖。当前 AST-only 路径不要求 GCC 存在；工具链缺失、Clang/libclang 版本不匹配、缺少 `loc.file` / call reference / member reference / `qualType` evidence，或配置的 compile database 不可读/格式错误时，`cipher2` 会显式失败。单文件目标源码 AST 失败会跳过该文件并记录 warning，不会使用 JSON dump 或 lightweight parser fallback。

## 初始化与重建

```bash
cipher2 init /path/to/repo
cipher2 init /path/to/repo --source-root src --profile debug
cipher2 init /path/to/repo --compile-database /path/to/compile_commands.json
cipher2 init /path/to/repo --no-progress
cipher2 init /path/to/repo --no-mcp-config
cipher2 init /path/to/repo --print-mcp-config
cipher2 rebuild /path/to/repo --json
```

`init` 首次建立 `.cipher/`；`rebuild` 离线全量重建并原子发布新 snapshot。两个命令都不进入交互式 TUI，不等待 Enter，不创建 `.cipher/inference/`。

`init` 默认向 stderr 展示抽取进度：source 总数、当前已处理数量、当前仓库相对文件、耗时，以及结束时的 facts/relatives/warnings/partial AST 摘要。stdout 不输出进度，因此 `--json` 和管道读取不受影响。stderr 是 TTY 时原地刷新；非 TTY 时退化为周期性整行日志。使用 `--no-progress` 可关闭进度；`--no-log` 只关闭结构化日志，不关闭终端进度。

全量 `init` / `rebuild` 的 per-file Clang 抽取由 `extractor.worker_count` 控制。省略或 `null` 表示按 CPU 数 auto，最大 32；显式 `1` 使用单 worker 子进程并保持串行调度语义，因此 target AST timeout 仍可 kill/restart。实际 worker 数仍受 source 数限制，不会超过本次可抽取 source 数。大于 1 时使用多个长期 worker 进程执行 libclang parse、cursor traverse 和 mapper，绕开 Python GIL。多个 worker 可以乱序完成，并只写 `.cipher/run/initializer-mapreduce/<run_id>/` 下各自独占的 run-local map 段；facts 和 pending `direct_call` evidence 流式进入 SQLite-backed reducer，relatives 在 worker 写段前按 `relative_id` 和 canonical line 指纹跳过本进程已写 exact duplicate，再由 initializer external merge 对残余 canonical line 按 id 去重、排序和 conflict 检测，storage sorted-unique 路径只做流式顺序/重复校验和 snapshot 写入。跨文件 `direct_call` resolver 在函数索引完成后按 SQLite pending shard 并行运行，补齐的 resolved relatives 进入同一 external merge。仓内共享头声明缓存和 relative dedup 表都只在单个 worker 进程内复用，不跨进程共享、不落盘。

文件级 libclang AST 采用 best-effort：某个文件因 timeout、malformed AST、空 TU 或 libclang C API 错误失败时，该文件不会进入 `source_inventory`，其余文件继续抽取，并通过 `clang_ast_failed` 定位；timeout warning 会写 `diagnostic_kind="timeout"`、`diagnostic_reason="timeout"` 和实际 `timeout_seconds`。若 libclang diagnostics 有 error/fatal 但 TU 仍可解析，cipher2 会接受可用节点，该文件进入 `source_inventory`，并通过 `clang_ast_partial` / `partial_ast_count` 告知结果为部分 AST；`diagnostic_reason` 使用 `diagnostic_error`、`diagnostic_fatal` 或 `diagnostic_error_and_fatal`。

未显式传 `--compile-database` 且配置为空时，`init` 会自动探测 `compile_commands.json`：优先检查仓库根、`build/`、`out/`，再检查这些目录的一层子目录和仓库浅层二级目录。找到后只把路径写入 `paths.compile_database`，不会在 config 阶段解析 JSON 内容。找不到时 `init` 仍会完成 snapshot，但 setup summary 会发出 `compile_database_not_found` warning，提示 `cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -B build`、`bear -- <build command>` 或显式 `--compile-database <path>`，因为缺少真实 include/macro flags 会降低 C AST 质量。

`init` / `rebuild` 完成 config 准备后会调用 initializer build readiness 预检，并写入 `initializer.build_readiness` 结构化事件。该预检只记录 compile database 是否配置、Clang/GCC readiness 和缺失输入计数，不改变 stdout/stderr 合同。

配置 `--compile-database` 或自动发现 compile database 后，code extractor 会读取 `compile_commands.json`，默认只抽取其中列出的仓内 translation unit；`--source-root` / `source_roots` 只会在这个构建 source set 上继续收窄，不会把未列入 compile database 的同后缀文件作为独立 TU 重新纳入。被这些 TU include 的仓内头文件仍会写入 source inventory 和 include graph，用于增量 header fanout，但不会用空 per-file flags 单独抽取。每个被抽取 source 会使用清洗后的 per-file flags 加入 libclang parse。全局 `extractor.code.clang_args` 仍用于 capability probe；在目标源码 parse 中它排在 per-file flags 之前，因此 per-file flags 对该 source 的重复语义优先。命中 entry 后，相对 include/pre-include/sysroot/framework path 参数会按 entry `directory` 转成绝对路径。compile database JSON 或 entry 格式错误会以 `malformed_compile_database` 阻断 init/rebuild。建议先完成目标工程构建或生成 compile database，再运行 `init` / `rebuild`。

初始化完成后，`init` 默认还会创建或合并仓库根 `.mcp.json`，作为 repo-local MCP project 配置。它只创建或替换 `mcpServers["cipher-2"]`，保留其它 server 和顶层字段，并使用运行 init 的 `sys.executable` 作为 `command`。已有 `.mcp.json` 格式错误时不会覆盖原文件，只会输出 setup warning；`--no-mcp-config` 可跳过写入，`--print-mcp-config` 可打印手工兜底片段。`cipher2` 不写仓库外客户端配置。

常用参数：

| 参数 | 说明 |
|---|---|
| `target` | 目标仓库根目录，必须存在且可读。 |
| `--source-root` | 限定抽取目录或文件，可重复；必须位于目标仓库内。 |
| `--profile` | 写入 `object_profile`，默认 `default`。 |
| `--compile-database` | 写入 `.cipher/config.yml` 的 `paths.compile_database`。 |
| `--no-progress` | 关闭 `init` 的 stderr 进度输出；不影响 stdout。 |
| `--no-mcp-config` | 跳过仓库根 `.mcp.json` 写入。 |
| `--print-mcp-config` | 输出可手工粘贴的 MCP 配置片段；`--json` 时进入 `setup.printed_mcp_config`。 |
| `--no-log` | 关闭本次命令的结构化日志。 |
| `--json` | 输出机器可读结果。 |

## 查看状态

```bash
cipher2 status /path/to/repo
cipher2 status /path/to/repo --json
```

`status` 只读取 `.cipher/`，不会执行 `init`、`rebuild`、extractor 或 snapshot 发布。没有 snapshot 时仍可运行，storage section 显示 `empty` 或稳定错误码。

human 输出固定为无 ANSI 的多行摘要：

```text
cipher-2 status: /path/to/repo
state: ready

storage: ready
  snapshot: sha256-abc123
  format: compact-jsonl-gzip  compression: gzip-1
  snapshot_bytes: 12.3MB  raw_bytes: 45.6MB  ratio: 27%
  facts: 1,234  relatives: 5,678
  field_read: 890  field_write: 456
  sources: 133  profiles: debug

log: warning
  events: 42  channels: cli, config, initializer, storage
  extractor workers: mode=bounded_pool count=4 ok=130 skipped=3
  errors: clang_ast_failed(3)
  clang: type_driven_ast=true loc=true call=true member=true qual_type=true
  compile_db: hit=120 miss=3 duplicate=1 ignored_outside_repo=2
  source_fallback: 2
  latest: 2026-05-27T11:36:47Z

incremental: ready
  base: sha256-abc123  overlay: -
  dirty: 0  pending: 0  failed: 0
```

`--json` 输出完整 `ToolsOverviewModel`，包含 `state`、`storage`、`log`、`incremental` 和 `errors`。stdout 只包含状态结果；失败诊断写 stderr。

incremental section 的 `state` 可能是 `ready`、`overlay`、`pending`、`stale` 或 `error`。`pending` 和 `stale` 都表示当前查询仍来自 base snapshot，只是结构化响应会附带新鲜度状态；`overlay` 表示查询读取 `base snapshot + temporary overlay`。overlay 空闲超过 `incremental.overlay_ttl_seconds`，或查询热路径发现 `snapshots/current` 已切到新 base snapshot 时，会以 warning 丢弃 overlay 并回到 base view；storage/read-index schema、dirty source compile command 和 source inventory toolchain 校验在启动对账、notify 和轮询扫描中执行。

## 配置

`.cipher/config.yml` 保存 schema 版本、compile database、Clang/libclang/GCC toolchain 输入、Clang 参数和在线临时增量配置。新建模板会包含每个字段的一句中文注释；下方省略注释只展示字段形状：

```yaml
schema_version: 1
paths:
  compile_database:
extractor:
  worker_count:
  code:
    clang_executable:
    libclang_library:
    gcc_executable:
    clang_args:
incremental:
  temporary_enabled: true
  poll_interval_ms: 500
  debounce_ms: 100
  worker_count: 1
  overlay_ttl_seconds: 600
  max_dirty_files: 500
```

旧版本中残留的 `graph.*` 或 `inference.*` 配置会被忽略并记录兼容警告，不会阻断初始化。
`extractor.worker_count` 只影响全量 init/rebuild，不影响在线临时增量的 `incremental.worker_count`。
`incremental.worker_count` 是 v1 保留兼容字段：配置仍会校验并进入 log/status 上报，但在线临时增量 active worker 固定为 `1`，不会改变 dirty source 抽取并行度。
`paths.compile_database` 只保存路径；参数解析、allowlist 清洗和命中/miss 统计在 code extractor 中完成。日志和 status 不展示完整 command、绝对路径、源码正文或环境变量。
`extractor.code.libclang_library` 通常应留空。code extractor 会先按 `clang_executable`、`llvm-config` 和平台默认路径自动定位同一工具链的 libclang；只有自动定位失败时才读取该显式路径作为 last-resort 逃生舱，并继续校验 Clang/libclang major 版本匹配。Python 侧只使用标准库 `ctypes` 调用 libclang C API，不需要额外 PyPI 运行时依赖。

## 字段与来源

C extractor 使用 Clang 的类型和声明引用 evidence 建立事实：

- 函数、类型和字段的 `object_id` 包含足够的 source 或 owner identity，同名 `static helper()` 和同名字段不会互相覆盖。
- `object_source` 优先来自仓库内 AST `loc.file` / `range.begin.file`；缺失、系统头文件或路径逃逸时回退到当前 translation unit。
- field fact 的 `object_name` 只显示字段名，例如 `size`；字段所属类型通过 `detail` 的 incoming `has_field`、payload 中的 `owner_name` / `owner_type_id` 和 source context 查看。
- 匿名 `struct/union` 字段使用 synthetic owner，例如 `Outer::<anonymous-union>@src/foo.c:2:3`，因此 `o->a` 这类匿名 union 字段访问也能落到稳定 field fact。
- `direct_call` 只来自 Clang call reference。跨文件调用会在所有文件抽取完成后用 pending evidence 补齐；补齐时优先匹配 referenced source，fallback 只接受 linkage-aware 唯一同名 function，其他 source 的 `static` / `internal` 函数不会被跨 translation unit 连边。
- 函数指针字段、全局变量和本地 slot 的赋值写为 `assigned_to`，间接调用写为 `dispatches_via`。函数指针类型识别使用 Clang 的 `qualType` / `desugaredQualType` evidence，因此 `typedef` 包裹的结构体成员函数指针也必须被识别。

## 生成文件

```text
.cipher/
  config.yml
  snapshots/
    current
    <snapshot_id>/
      facts.jsonl.gz
      relatives.jsonl.gz
      source_inventory.jsonl.gz
      read_index.sqlite
      manifest.json
      stats.json
  log/
  run/
```

snapshot schema v5 使用 `compact-jsonl-gzip`：数据文件是 gzip 压缩后的 canonical JSONL，manifest 中的 hash 仍基于未压缩 line stream。`read_index.sqlite` 是派生的持久查询索引，当前 schema v6 使用整数代理键压缩 relatives endpoint 和 relative id，用于降低 MCP 首次查询冷启动耗时和索引体积；它不参与 snapshot identity。`stats.json` 和 `manifest.json` 保持未压缩，方便 `cipher2 status` 和人工检查。新 snapshot 不再写 `graph_objects.jsonl`、`graph_relatives.jsonl` 或 `graph_derived_from.jsonl`；旧 snapshot 中存在这些文件时，查询会忽略它们。v4/v3 snapshot 不做回读兼容，遇到 schema、文件格式不匹配、旧 read index schema、read index 缺失/损坏时执行 `cipher2 rebuild /path/to/repo` 重建。

## MCP 查询

MCP 是 v1 唯一对外查询表面，只提供 `search` 和 `detail`。

### 注册到 MCP 客户端

`cipher-2` 只支持本地 stdio MCP，不监听端口，不提供 HTTP 端点。当前没有 `cipher2 serve` CLI；客户端配置需要调用实际已实现的 Python stdio 入口 `cipher2.mcp.serve_stdio`。

Claude Desktop、Cursor 等 MCP 客户端可使用以下配置形状：

```json
{
  "mcpServers": {
    "cipher-2": {
      "command": "/path/to/python",
      "args": [
        "-c",
        "from cipher2.mcp import serve_stdio; raise SystemExit(serve_stdio('/path/to/repo'))"
      ]
    }
  }
}
```

配置要求：

- `/path/to/python` 必须是能 `import cipher2` 的 Python 解释器，例如项目虚拟环境的 `bin/python`。
- `/path/to/repo` 必须先运行过 `cipher2 init /path/to/repo` 或 `cipher2 rebuild /path/to/repo`，并存在 `.cipher/snapshots/current`。
- MCP server 只读当前 snapshot 和临时 overlay；不会自动执行 `init`、`rebuild`、编译或源码扫描。每次 `search` / `detail` 都会先通过 incremental `current_view()` 做轻量 overlay guard；不会在查询热路径解析 compile database、重扫 source inventory 或运行 Clang/libclang probe。dirty planning 期间返回 base 结果并标注 `view_state="pending"`，无法安全发布 overlay 但 base 仍可查时标注 `view_state="stale"`。

### Agent 系统提示指导

面向弱模型或会自行调用 grep/read 的 agent，仓库提供
[`docs/cipher-agent-system-prompt.md`](cipher-agent-system-prompt.md) 作为可直接附加到系统提示的使用指导。集成方自行通过
append-system-prompt 文件机制注入；MCP server 不会自动注入，也不新增 tool 或参数。

该指导只承载 agent 行为约束：关系查询结果应按当前 indexed snapshot 解读，`too_broad` 的总数和显著子集是有界答案，且不应用 grep、`name:` 猜测或自写 parser 补全关系结果。下面的 MCP 查询章节仍是关系谓词、`too_broad` 响应和“完整关系审计不作为 MCP public tool 暴露”的权威说明。若使用方需要 source-complete 口径，应先查看 `cipher2 status` / views 中的抽取告警。

### Python API

```python
from cipher2.mcp import open_mcp_server

server = open_mcp_server("/path/to/repo")
search = server.search("free member", limit=20)
detail = server.detail(search.results[0].object_id, budget="normal")
```

`search(query, limit)` 在 FACT 层执行大小写不敏感的分词交集匹配。`"free member"` 会拆成 `free` 和 `member`，只有两个 term 都命中的 fact 才会返回。排序会提升精确同名的 `type` / `function` / `global` 等定义类 fact，避免大量同名 `field` fact 填满默认结果窗口。

`search` 也支持在同一个 `query` 字符串中使用关系型谓词，沿已有 `FactRelative` 边返回相关 FACT，不新增 MCP tool 或参数：

```text
search("NullableDatum value") -> 取 field result.object_id
readers:<field_object_id>
writers:<field_object_id> file:numeric.c
accessors:<field_object_id> file:list.c
dispatches_via:ExecProcNode
callers:add_var
callees:numeric_add
callees:in_range_numeric_numeric depth:2
callers:eval_const_expressions depth:2
reachable:in_range_numeric_numeric->add_var
```

`readers:` / `writers:` / `accessors:` 的 anchor 是 field fact。可靠工作流是先用普通 `search` 搜字段名和 owner 关键词，取返回 field fact 的 `object_id`，再调用 `readers:<field_object_id>` / `writers:<field_object_id>` / `accessors:<field_object_id>`，或直接 `detail(<field_object_id>)` 一次查看 `field_readers` / `field_writers` 桶。不要依赖自己合成的 `Type.field` 字符串：field fact 的 `object_name` 只保存裸字段名，owner alias 只是兼容消歧路径，可能因 typedef、匿名 owner 或 payload 模糊匹配落空。`writers:<field_object_id>` 若返回 `total=0`，可以在问题允许读访问兜底时改用 `accessors:<field_object_id>`。`dispatches_via:<field_object_id>` 返回该函数指针字段经 `assigned_to` 解析出的候选函数；`callers:` / `callees:` 的 anchor 是 function。`callers:` / `callees:` 缺省只查一跳；追加 `depth:<N>` 可以在内置上限内做有界传递闭包，例如 `callees:in_range_numeric_numeric depth:2` 会返回 2 跳内被调函数，并在每条 slim result row 上标出 `hop`。call closure 和 `reachable:A->B` 都沿 outgoing call edge 判断可达性：`direct_call` 直接连函数，函数指针 dispatch 由同一 slot 上的 `dispatches_via` + `assigned_to` 合成，path 中该跳标为 `dispatches_via`。命中时返回 `reachable=true` 和一条最短 `path`；path node 可带 `condition`，表示这一跳调用点的局部分支/守卫条件，多跳复合条件是各跳非空 condition 的逻辑 AND。未命中但 frontier 已耗尽时返回 `complete=true`，达到深度或成本边界时返回 `complete=false`，不把 bounded no-hit 说成全局无路径。

`depth` 只支持 `callers:`、`callees:` 和 `reachable:`；`depth:0`、负数、非数字、重复 `depth:` 或用于 `readers:` / `writers:` / `accessors:` / `dispatches_via:` 时，响应会要求改写 query，不会静默退化为一跳。传递闭包有固定访问和 frontier 预算；预算耗尽时返回 `budget_exhausted=true` 和可执行收窄建议，例如降低 `depth` 或加 `file:`。

`file:<substring>` 匹配返回端点的 source file，匹配前会去掉 `object_source` 右侧 `:<line>`；`caller:<substring>` 与 `name:<substring>` 都匹配返回端点的 `object_name`，只适合检查已知的特定端点，不应用来猜函数名枚举。关系型 `limit` 硬上限是 50；如果关系型结果数大于 `limit`，响应会返回 `status="too_broad"`、准确 `total`、`available_filters`、`examples` 和 slim `results` 子集。未带 `file:` 时会推荐先加文件过滤，例如 `readers:<field_object_id> file:numeric.c`。已带 `file:` 但仍超过上限时，返回结果就是最显著子集，调用方应连同 `total` 一起作为有界答案呈现，而不是继续枚举候选函数名。`status="needs_refinement"` 会返回 `anchor_candidates`，并在 message 中列出候选 `(object_id, owner, source)`；调用方应选一个候选 `object_id` 重试，不要继续猜 `Type.field`。关系型 `results` 不再复制完整 fact summary，也不通过 `top_by_salience` 重复同一批端点；普通文本 search 和 `detail.relative_preview.buckets` 的形状不变。

`detail(fact_id, budget)` 返回：

- FACT 摘要。
- bounded payload。
- bounded source context。
- relative preview，包括 incoming/outgoing relation kind 计数、按方向和关系类型分桶的 `buckets`，以及兼容旧客户端的最多 8 条扁平 `relatives` 样本。

`relative_preview.buckets` 是权威关系预览形状；顶层 `relative_preview.relatives` 只用于旧客户端降级展示，不再复制所有 bucket relatives。常见桶名包括 `callers`、`callees`、`field_readers`、`field_writers`、`fields` 和 `field_owner`。每个桶包含 `total_count`、`shown_count` 和 `truncated`，因此枚举调用方或字段访问者时可以判断是否还有未展示关系。桶内会按 endpoint 归并重复 call-site：`instances` 表示该摘要覆盖的真实 relation 数，`conditions` 是去重后的条件集合，`endpoint_name` / `endpoint_profile` / `endpoint_source` 可直接用于枚举答案；截断时优先展示来自不同 `endpoint_source` 文件的 endpoint。`detail(field_id)` 能通过 incoming `has_field` 看到字段 owner，并通过 incoming `field_read` / `field_write` 看到读写该字段的函数。高扇入字段的 `field_readers` / `field_writers` 可能只展示最相关的预算内子集；此时 `total_count` 是完整规模信号，`shown_count` 是本次 preview 返回数量，`truncated=true` 不等同于覆盖缺失。

`budget` 还对序列化后的 `DetailResponse` 施加硬上限：`small <= 8KB`、`normal <= 32KB`、`large <= 128KB`。触顶时运行时先收缩顶层扁平兼容样本，再减少 bucket 返回条数，再缩小 source context，最后缩小 payload；各级 `total_count` 保持真实规模。需要枚举有用子集时，使用关系型 `search` 加 `file:` / `caller:` 过滤继续收敛；完整关系审计仍由 storage 内部 API 支持，不作为 MCP public tool 暴露。

## 错误处理

常见错误：

| 错误码 | 处理方式 |
|---|---|
| `clang_unavailable` | 配置或安装可执行 Clang。 |
| `clang_capability_failed` | 使用支持类型驱动 AST evidence 的 Clang；正式支持 LLVM Clang >= 16 和 Apple Clang >= 15。log/views 中的 `missing_evidence` 会指出缺失项。 |
| `clang_ast_failed` | 单文件被跳过；确认目标仓库已构建好，compile database、include path 和 generated headers 可用。 |
| `clang_ast_partial` | 单文件已用部分 AST 抽取；优先补齐 include path、generated headers 或 compile database，以减少遗漏。 |
| `libclang_unavailable` | libclang 自动定位失败，或显式 `extractor.code.libclang_library` 不可读；优先确认 Clang/LLVM 安装完整，必要时再配置显式库路径。 |
| `libclang_version_mismatch` | `clang_executable` 与加载到的 libclang major 不匹配；让二者来自同一个 Clang/LLVM 工具链。 |
| `field_decl_without_fact_count` / `field_access_unresolved_count` / `field_access_scan_truncated_count` | field coverage 仍有缺口；查看 `cipher2 status --json` 的 log section，确认匿名 record、type materialize 或字段访问递归扫描是否触发上限。 |
| `unresolved_dispatch_slot_count` / `unresolved_dispatch_function_count` | 函数指针 dispatch coverage 仍有缺口；确认目标仓库已生成完整 compile database，且相关函数指针字段、变量和目标函数有 Clang declaration/type evidence。 |
| `gcc_unavailable` | 仅在显式配置的 GCC 路径不可执行时触发；修正或清空 `extractor.code.gcc_executable`。 |
| `compile_database_unreadable` | 检查 `paths.compile_database` 是否可读且不在 `.cipher/` 内。 |
| `malformed_compile_database` | 修复 compile database JSON、entry 字段类型或无法 shell split 的 `command`。 |
| `unsupported_schema_version` / `snapshot_corrupt` / `manifest_mismatch` | 当前 snapshot 不是 v5 gzip + read index v6 格式、缺文件、SQLite index 损坏、gzip 损坏或 hash 不匹配；运行 `cipher2 rebuild /path/to/repo`。 |
| `invalid_source_root` | 确认 `--source-root` 位于目标仓库内。 |
| `unknown_tool` | MCP 只支持 `search` 和 `detail`。 |

日志位于 `.cipher/log/*.jsonl`。日志和 views 会脱敏 query、路径和敏感字段，不保存源码正文或 traceback。
