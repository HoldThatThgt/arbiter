# examples

## 路径职责

本目录存放小型示例仓库和 fixtures。

## 应放入这里的内容

- 用于代码 FACT 抽取的最小 C 项目。
- 示例 compile databases。
- `cipher2 init`、stdio MCP 和 TUI view model 的预期用户流程。

## 不应放入这里的内容

- 大型第三方仓库。
- 作为普通示例提交的生成 `.cipher/` 输出。
- 对应抽取器存在前的 Concept 或 git 示例。

## 设计计划

examples 应足够小，可以在 CI 中运行，并聚焦真实 C 模式：direct calls、globals、macros、function pointer assignment、dispatch 和 hook evidence。

## 验证计划

未来 example tests 应初始化临时副本，并断言生成文件只出现在临时示例仓库的 `.cipher/` 下。
