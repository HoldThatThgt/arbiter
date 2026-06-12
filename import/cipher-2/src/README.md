# src

## 路径职责

`src/` 存放 `cipher-2` 的 Python 包源码。当前运行时是 FACT-only：初始化负责收集 facts/relatives/source inventory，storage 负责 v5 gzip snapshot、持久 read index、关系 BFS 和 overlay，MCP 只公开 `search` 与 `detail`。

## 目录结构

```text
src/
  cipher2/
    cli.py
    __main__.py
    common/
    config/
    initializer/
    incremental/
    mcp/
    storage/
    tools/
```

目标状态不包含 `cipher2.graph` 和 `cipher2.initializer.inference` runtime。实现删除前若目录仍存在，不得继续扩展其能力。

## 模块边界

- `cipher2.cli`：`cipher2 init/rebuild` 参数解析、结果渲染、config 写入和 initializer API 包装；不进入交互式 TUI。
- `cipher2.config`：`.cipher/config.yml`、toolchain、incremental 和路径安全；忽略旧 `graph.*` / `inference.*`。
- `cipher2.initializer`：编排 libclang AST 抽取和 storage snapshot 写入，并记录 init/rebuild 的 `collect`、`extract`、`reduce`、`resolve`、`relative_merge`、`snapshot_write`、`read_index` 阶段耗时。
- `cipher2.initializer.extractor.code`：使用标准库 `ctypes` 薄封装 libclang C API，执行类型驱动 capability probe、compile database per-file flags 归一、process worker 并行、partial AST 接受策略和跨文件 direct_call 后处理，产出 C facts、FactRelative、source inventory、field fact 覆盖和 field access 关系；不引入 PyPI 运行时依赖。
- `cipher2.storage`：v5 `compact-jsonl-gzip` FACT snapshot、持久 SQLite read index、multi-term search、关系谓词 BFS、FactView、relative preview、temporary overlay。
- `cipher2.incremental`：在线临时增量 overlay。
- `cipher2.mcp`：本地 stdio MCP `search` / `detail`，其中 `search` 承载 FACT 分词、关系谓词、传递闭包和可达性查询，`detail` 对 payload、source context、bucketed relative preview 和序列化响应字节数施加预算。
- `cipher2.tools.log`：追加式 JSONL 事件，并在摘要中保留最近一次 `init.stage` 阶段事件。
- `cipher2.tools.views`：storage/log/incremental 只读 view model，并向 `cipher2 status` 暴露最近一次 init/rebuild 阶段耗时表。
- `cipher2.common`：跨模块窄类型，例如 `JSONValue`。

## 约束

- C 场景不得启用 lightweight parser、JSON AST dump 或 subprocess mapper fallback。
- 生成文件只能写入目标仓库 `.cipher/`。
- public MCP 不暴露 `impact`、`relations` 或 Graph scope。
- 日志、views、MCP 响应不得泄漏源码正文、绝对 target path、完整 query、traceback 或 secret。
