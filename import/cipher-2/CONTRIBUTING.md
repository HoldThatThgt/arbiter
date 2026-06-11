# 贡献指南

## 开发原则

`cipher-2` 是面向 CIPHER v1 的 `TheFact + FactRelative + source inventory` FACT-only 实现仓库，并支持在线临时增量 overlay。Graph projection、声明式 inference rules 和 MCP `impact` 不属于当前运行时边界。所有运行时行为必须保持在已公开的模块 README 规格内；如果实现需要改变规格，先更新设计记录和对应 README，再修改代码。

所有项目文档使用中文。路径、命令、协议名和字段名可以保留英文原文。用户文档、维护文档和 README 应面向真实使用者与维护者，避免记录个人开发环境或本地私有配置。

源码组织：`src/` 下每个代码文件保持 < 2000 LOC。超出时按职责拆分为聚焦模块，包的 `__init__.py` 只做公开名 re-export、不堆实现；单个类自身超限时按职责再内部拆分。

## 分支与 Pull Request

使用短生命周期功能分支，提交信息保持 Conventional Commit 风格，例如：

```text
feat: add fact store
test: cover config containment
docs: document cli init runtime
```

PR 必须说明影响的 v1 边界、列出已运行命令、关联 issue 或设计记录；只有 TUI 或用户可见界面变化需要截图。不要直接向 `main` 推送功能改动。

## 开发流程

功能开发采用三阶段 PR 流程：

1. 设计草稿 PR：开发前先在 `docs/design-drafts/` 写设计，说明模块定位、规格约束、数据结构、接口流程、并发控制、文档递归更新、可观测性和测试门禁。
2. README 搬迁 PR：设计合入后，把草稿内容搬迁到对应模块 README，并从被改动模块递归更新到顶层文档，确认语义没有漂移。
3. 实现 PR：文档合入后按 TDD 开发，先提交失败测试并确认失败，再实现最小代码并补齐覆盖率、场景和性能门禁。

每个阶段必须独立提 PR。PR 有检视意见时，必须修订同一个 PR，直到合入或明确放弃；不得用口头确认、评论回复、本地提交或直接 push `main` 代替合入。

任何新功能都必须在 `tools/log` 增加可观测事件，在 `tools/views` 呈现核心统计信息，并为这些可观测信息增加专门用例，覆盖正常记录、失败记录、空状态、聚合统计、截断或限额、异常恢复和展示字段稳定性。

## 文档同步

新功能从被改动模块 README 开始递归更新文档，直到顶层 README 或相关维护文档完整描述用户入口、配置、数据结构、错误语义、可观测性和测试门禁。Bug 修复必须遵守现有文档的规格与约束；如果需要放宽或改变规格，按功能变更处理。

新增或修改命令入口时，必须同步 `README.md`、`docs/user-guide.md`、`src/README.md`、`src/cipher2/README.md` 和 `tests/README.md`。`cipher2 init`、`cipher2 rebuild` 的入口 smoke test、console script 暴露方式、stdout/stderr 格式和可观测事件都必须在文档中可追溯。`--interactive` 这类非持久参数不得改变默认 fire-and-forget 行为，且不能与 `--json` 同用。

## 测试与门禁

提交前至少运行：

```bash
git diff --check
PYTHONPATH=src python3 -m unittest discover -s tests
```

涉及具体模块时，还必须运行对应性能门禁脚本，例如 `scripts/storage_performance_gate.py`、`scripts/initializer_performance_gate.py`、`scripts/mcp_performance_gate.py`、`scripts/log_performance_gate.py`、`scripts/views_performance_gate.py` 或 `scripts/incremental_performance_gate.py`。

涉及 `cipher2 init`、`cipher2 rebuild` 或命令入口时，还必须运行 `PYTHONPATH=src python3 scripts/cli_performance_gate.py`，并覆盖 `python -m cipher2 --help`、`python -m cipher2 --version`、`python -m cipher2 init <tmp-repo> --json`、`python -m cipher2 rebuild <tmp-repo> --json` 以及 console script smoke test。

测试必须使用临时目标仓库生成 `.cipher/` 输出，不得把 snapshot、log 或其他生成产物提交到源码树。
