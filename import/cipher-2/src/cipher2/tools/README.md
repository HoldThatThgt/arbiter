# tools

## 路径职责

本包是 `cipher-2` 的工具模块集合，放置服务于核心 FACT、FactRelative、source inventory 与在线临时增量流程的辅助能力。

## 子模块

- `log/`：追加式 JSONL 事件、读取、脱敏和摘要，包括 compile database 命中、miss、重复 entry、参数清洗、partial AST 和 direct call resolution 统计。
- `views/`：读取 storage、log、incremental 状态并生成人类可读 view model，包括 type-driven Clang capability、compile database、source fallback、unresolved call、partial AST 和 direct call resolution 统计。

## 模块边界

tools 不承担 initializer、storage 或 MCP 的业务逻辑，不修改 FACT store，不生成事实或关系。它只消费已有状态并提供可观测和展示能力。

`views` 不读取 Graph projection，不展示 inference section，也不负责 init/rebuild 的交互式教程。

## 测试门禁

- log schema、恢复、摘要、脱敏、截断。
- view model 聚合、空状态、异常状态、section 隔离。
- storage/MCP/incremental 调用时的可观测信息看护。
- extractor type-driven capability、missing evidence、compile database、source fallback、partial AST 和 direct call resolution 呈现看护。
