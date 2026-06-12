# cipher-2

`cipher-2` 是 CIPHER v1 的 FACT 运行时实现。它面向本地 C 代码仓库，通过类型驱动的 libclang 进程内 AST 遍历收集 `TheFact`、`FactRelative` 与 source inventory，写入目标仓库 `.cipher/`，并通过本地 stdio MCP 提供 `search` 和 `detail` 查询。

## 功能范围

v1 数据流：

```text
目标仓库
  -> cipher2 init/rebuild
  -> .cipher/config.yml
  -> initializer/extractor/code (type-driven Clang AST capability probe + bounded per-file process worker pool + worker-local exact relative dedup + facts SQLite reducer + relatives external merge)
  -> compact FACT + FactRelative + source inventory snapshot
  -> 在线临时增量 overlay
  -> stdio MCP search/detail
  -> tools/log (`init.stage` + storage/MCP/incremental events)
  -> tools/views (storage/log/incremental status model)
  -> cipher2 status
```

已实现目标边界：

- 只分析目标仓库内的 C 源码和头文件事实；C 抽取固定使用类型驱动 Clang AST。
- C 抽取只信任 Clang AST 的 `loc.file`、`referencedDecl`、`qualType` 等类型和声明引用 evidence，不用字符串模式推导符号关系。
- 配置 compile database 时，C 抽取默认把其中仓内 entry 作为权威构建 translation unit set；`source_roots` 只在该集合上继续收窄，不再 rglob 未列入 compile database 的同后缀文件作为独立 TU。仓内被 include 的头文件仍进入 source inventory 和 include graph，用于增量 header fanout，但不会作为独立 TU 用空 flags 抽取。被抽取 source 使用 allowlist 清洗后的 per-file flags；相对 include/pre-include/sysroot/framework 路径按 entry `directory` 归一为绝对路径；目标 libclang parse 内置 `-ferror-limit=0`。
- 跨文件 `direct_call` 由 Clang call reference evidence 后处理补齐；唯一同名 fallback 必须感知 linkage，不能把其他 source 的 `static` / `internal` 函数连成跨 translation unit 调用。
- code fact 的 `object_source` 优先使用仓库内 AST source location；同名函数、类型和字段由 source 或 owner identity 区分。
- field fact 的 `object_name` 只保存字段名；匿名 `struct/union` 字段也必须通过 synthetic owner 创建 field fact，字段归属通过 incoming `has_field`、payload owner 字段和 source context 呈现。
- 初始化默认只写目标仓库 `.cipher/` 和仓库根 `.mcp.json`；`.mcp.json` 是 repo-local MCP project 配置，不改写源码或仓库外客户端配置。
- 只提供本地 stdio MCP，不提供 HTTP MCP。
- 持久化 `TheFact`、`FactRelative` 和 source inventory；不保留 Graph projection、Concept 或 Git facts。
- `search` 在 FACT 层做分词交集匹配，并支持一跳关系查询、`dispatches_via:<field>` 候选查询、`callers:` / `callees:` 有界传递闭包和 `reachable:A->B` 有界可达性；call closure 包含 `direct_call`，也会把同一 slot 上的 `dispatches_via` + `assigned_to` 合成为 dispatch 跳，`reachable` path node 可带每跳局部 `condition`；`detail` 返回 bounded payload、source context 和按桶归并、排序、多样化选择的 relative preview，且按 `small/normal/large` 对序列化响应施加 8KB/32KB/128KB 上限，顶层扁平 `relatives` 只保留小兼容样本。
- 在线增量只使用临时 overlay，不移动 `snapshots/current`；离线永久更新使用全量 `rebuild`。临时 overlay 有空闲 TTL，并在查询热路径发现 `snapshots/current` 切换时 fail-closed 回到 base view；storage/read-index schema、dirty source compile command 和 source inventory toolchain 校验在启动对账、notify 与轮询扫描中执行。dirty planning 或 stale 场景通过 `view_state="pending"` / `"stale"` 暴露 base-only 查询状态。

## 安装与快速开始

开发环境需要 Python 3.9 或更新版本。初始化 C 仓库还需要可执行 Clang 及同一工具链的 libclang；Python 侧只用标准库 `ctypes` 薄封装 libclang C API，不引入 PyPI 运行时依赖。libclang 默认按 `clang_executable`、`llvm-config` 和平台默认路径自动定位；只有自动定位失败时才读取 `extractor.code.libclang_library` 作为 last-resort 逃生舱。Clang/libclang 必须通过类型驱动 AST capability probe，当前 AST-only 路径不要求 GCC 存在。Clang 或 libclang 不可用、版本不匹配、缺少 `loc.file` / call reference / member reference / `qualType` evidence、配置的 compile database 不可读或格式错误、源码输入缺失时必须显式失败。单文件 AST malformed/timeout 会跳过该文件并记录 `clang_ast_failed`；target AST timeout 基础为 120 秒，并按文件大小增长以覆盖超大 C 文件。若 libclang diagnostics 存在 error/fatal 但 TU 仍可解析，则接受可用 AST 并记录 `clang_ast_partial` warning，`diagnostic_reason` 使用 `diagnostic_error` / `diagnostic_fatal` / `diagnostic_error_and_fatal`。文件级 warning 会进入 `InitSummary.errors`，CLI JSON 成功输出存在 warning 时会附带 `warnings` 清单。任何场景都不会降级到 JSON dump 或 lightweight parser。

```bash
python3 -m pip install -e .
cipher2 --version
cipher2 init /path/to/repo --json
```

常用命令：

```bash
cipher2 init /path/to/repo
cipher2 init /path/to/repo --source-root src --profile debug
cipher2 init /path/to/repo --compile-database /path/to/compile_commands.json
cipher2 init /path/to/repo --no-progress
cipher2 init /path/to/repo --no-mcp-config
cipher2 init /path/to/repo --print-mcp-config
cipher2 init /path/to/repo --no-log
cipher2 rebuild /path/to/repo --json
cipher2 status /path/to/repo
cipher2 status /path/to/repo --json
```

初始化完成后，目标仓库会生成 `.cipher/config.yml`、`.cipher/snapshots/`、`.cipher/log/`、`.cipher/run/` 和仓库根 `.mcp.json`。snapshot 使用 v5 `compact-jsonl-gzip` 格式，实际文件为 `facts.jsonl.gz`、`relatives.jsonl.gz`、`source_inventory.jsonl.gz` 和持久查询索引 `read_index.sqlite`；read index schema v6 是派生的整数代理键投影，不参与 snapshot identity，旧 v5 read index 或 v4/v3 snapshot 需要通过 `rebuild` 重建。命令行参数只影响单次运行；显式 `--compile-database` 会写入既有配置项 `paths.compile_database`，未显式传参且配置为空时 `init` 会自动探测仓库根、`build/`、`out/` 和浅层目录中的 `compile_commands.json` 并写入路径。找不到 compile database 时 `init` 不阻断，但会在 setup summary 中输出可操作 warning，提示 CMake/Bear 或 `--compile-database` 用法；`init/rebuild` 完成 config 准备后会调用 initializer build readiness 预检并写入 `initializer.build_readiness` 事件。全量 `init` / `rebuild` 的 per-file Clang 抽取由 `extractor.worker_count` 控制，省略或 `null` 表示按 CPU auto 且上限 32，显式 `1` 使用单 worker 串行调度，但 target AST 仍在可 kill/restart 的 worker 子进程内执行；大于 1 时使用多个长期 worker 进程绕开 Python GIL。实际 worker 数仍受 source 数限制，不会超过本次可抽取 source 数。worker 可乱序完成并写 `.cipher/run/initializer-mapreduce/<run_id>/` run-local map 段；facts 和 pending `direct_call` evidence 继续进入 SQLite-backed reducer，relatives 段写入前由当前 worker 按完整 `relative_id` 和 canonical line 指纹跳过已写 exact duplicate，再按 `relative_id` 排序并写 sidecar，initializer 对残余原始 canonical line 做流式 k 路归并、按 id 去重和 conflict 检测，不再在主进程 full-parse 每条 relative payload 或把 relatives 重 spool 到 SQLite。worker-local relative dedup 不跨进程、不落盘，达到内部内存上限后只继续过滤已追踪 id，未追踪 id 仍写段并交给 external merge 兜底；同 id 非 exact payload 在 worker 或 external merge 任一路径都必须 fail-closed 为 `map_reduce_conflict`。storage 在该路径只做流式顺序/重复校验、gzip/hash/read-index 写入，不再二次 SQLite re-sort。跨文件 `direct_call` resolver 在 fact reducer 完成后用只读函数索引按 SQLite pending shard 并行补齐，resolved relatives 进入同一 external merge。每个 worker 进程独立缓存它已物化的仓内共享头声明，后续由同一进程处理的 TU 可跳过已物化头子树并用 resolver seed 解析当前 TU relation；该缓存不跨进程共享、不落盘、不改变 CLI/MCP/storage schema。单文件 worker timeout 后 coordinator 会 kill/restart 对应 worker，并把该 source 映射为 `clang_ast_failed` + `diagnostic_kind="timeout"` warning；其他 source 继续抽取。`init` / `rebuild` 始终保持非交互，不等待用户输入，不创建规则工作区。`init` / `rebuild` 结束摘要包含 `collect`、`extract`、`reduce`、`resolve`、`relative_merge`、`snapshot_write`、`read_index` 中实际发生阶段的耗时；同一数据也以 `init.stage` 写入 `.cipher/log/initializer.jsonl`。`init` 默认向 stderr 输出 source 进度、当前文件和耗时，TTY 原地刷新，非 TTY 周期写整行；stdout 仍只保留 human 或 `--json` 结果，`--no-progress` 可关闭进度。

`paths.compile_database` 只保存路径。config 模块不解析 `compile_commands.json` 内容；运行时的 code extractor 会读取 `compile_commands.json`，支持 `arguments` 和 `command` entry，把仓内 entry 作为默认抽取 TU set，把全局 `clang_args` 放在 per-file flags 之前，并丢弃 response file、plugin、输出、链接和未 allowlist 的参数。libclang 不启动 per-file subprocess，因此命中 entry 的相对路径参数会按 entry `directory` 转成绝对路径。compile database 格式错误会阻断 init/rebuild；未列入 compile database 的仓内源码默认不会被独立抽取，已被 TU include 的仓内头文件仍会进入 source inventory。

`cipher2 status` 是只读状态命令，读取 `tools/views` 聚合的 storage、log 和 incremental 统计。它不会运行 extractor、不会自动初始化仓库、不会写 snapshot；human 输出面向终端阅读并展示最近一次 init/rebuild 阶段耗时表，`--json` 输出完整 `ToolsOverviewModel`。`incremental.worker_count` 是 v1 保留兼容配置：会校验并在 `incremental.poll_started` 中上报 configured value，但在线临时增量 active worker 固定为 `1`。

## MCP 客户端配置

`cipher-2` 是本地 stdio MCP server，不提供 HTTP 端点。当前没有 `cipher2 serve` CLI；MCP 客户端需要使用安装了 `cipher-2` 的 Python 解释器调用 `cipher2.mcp.serve_stdio`。`cipher2 init /path/to/repo` 默认会在仓库根创建或合并 `.mcp.json`，保留其它 server，并用运行 init 的 `sys.executable` 作为 `command`，避免手写成未安装 cipher-2 的 `python`。使用 `--no-mcp-config` 可跳过写入；使用 `--print-mcp-config` 可在 stdout/JSON 中打印兜底配置片段。

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

`/path/to/python` 应指向安装了本包的 Python，例如虚拟环境的 `bin/python`；`/path/to/repo` 是已经生成 `.cipher/snapshots/current` 的目标仓库。`init` 不写任何仓库外客户端配置路径。

## 文档

- [用户手册](docs/user-guide.md)：安装、初始化、配置、查询和输出说明。
- [维护手册](docs/maintenance-guide.md)：开发流程、测试门禁、发布前检查和故障处理。
- [架构与文档索引](docs/README.md)：v1 架构边界和模块 README 索引。
- [Schema 说明](docs/schema.md)：FACT、FactRelative、source inventory 和目标 snapshot layout。
- [贡献指南](CONTRIBUTING.md)：分支、PR、文档同步和测试要求。
- [检索质量评测](benchmarks/retrieval/README.md)：离线复测 `search` + `detail` preview、store full 天花板和弱模型 A/B 回归。

模块级规格以 `src/cipher2/*/README.md` 为准。修改运行时行为时，必须从受影响模块 README 开始递归更新文档。

## 验证

提交前运行：

```bash
git diff --check
PYTHONPATH=src python3 -m unittest tests.test_live_libclang_smoke
PYTHONPATH=src python3 -m unittest discover -s tests
```

`tests.test_live_libclang_smoke` 会在本机缺 Clang/libclang capability 时 skip，但 CI 默认安装并要求真实 ctypes libclang backend smoke 实际执行。GitHub Actions `CI` 会在 PR 和 `main` push 上运行 `git diff --check`、全量 `py_compile`、CLI smoke test、live libclang smoke 和全量 unittest。性能门禁通过 `workflow_dispatch` 手动触发，选择 `run_performance=true` 后运行全部 `scripts/*_performance_gate.py`。

涉及具体模块时，还需要运行对应性能门禁，例如：

```bash
PYTHONPATH=src python3 scripts/initializer_performance_gate.py
PYTHONPATH=src python3 scripts/storage_performance_gate.py        # 含压缩率和冷启动索引耗时
PYTHONPATH=src python3 scripts/storage_relative_performance_gate.py # 含 relative-heavy 压缩率和冷启动索引耗时
PYTHONPATH=src python3 scripts/mcp_performance_gate.py
PYTHONPATH=src python3 scripts/mcp_relative_performance_gate.py
PYTHONPATH=src python3 scripts/clang_extractor_performance_gate.py
PYTHONPATH=src python3 scripts/cli_performance_gate.py
PYTHONPATH=src python3 scripts/incremental_performance_gate.py
```

检索呈现或关系抽取变更完成后，可手动运行离线检索质量基准。该工具只读取已初始化目标仓库的锁定 snapshot，不执行 init/rebuild，不进入默认 CI 硬门禁：

```bash
PYTHONPATH=src:. python3 -m benchmarks.retrieval.run --manifest /path/to/retrieval-manifest.json --output /tmp/cipher2-retrieval-report
```

## 运行时约束

- 除 `cipher2 init` 默认创建或合并仓库根 `.mcp.json` 外，不把生成文件写入目标仓库 `.cipher/` 之外；不写仓库外客户端配置。
- 不实现 Graph projection、Inference rules、MCP `impact`、Concept、Git extraction、HTTP MCP 或公开 `relations` tool。
- 不在 C 场景启用 lightweight parser；Clang/libclang 不可用、版本不匹配或类型驱动 AST capability probe 失败时必须阻断，不得回退到 JSON dump、subprocess AST mapper 或模式匹配 mapper。单文件目标 AST 失败只能作为可观测 warning 跳过该文件，不得静默丢失或降级。
- 不把在线临时增量写入持久 snapshot；临时 overlay 只能位于目标仓库 `.cipher/run/incremental/`。
- 不回读 v4/v3 snapshot；格式不匹配、`read_index.sqlite` 缺失或 metadata mismatch 时通过 `cipher2 rebuild` 生成 v5 gzip snapshot 和持久读索引。
- 不在日志或 view model 中泄漏源码正文、绝对目标路径、完整 query、traceback、secret 或 provider internals。
