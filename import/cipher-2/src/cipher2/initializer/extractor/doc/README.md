# initializer/extractor/doc

## 路径职责

本包预留给未来的文档和 Concept 事实抽取。

## 应放入这里的内容

- 未来 Markdown、CSV、source comment、diagram 和 Concept 事实抽取。
- 未来确定性 document evidence 加载。
- 未来可选的 LLM-assisted evidence 抽取，但必须受 FACT provenance 约束。

## 不应放入这里的内容

- v1 运行时代码。
- 代码事实抽取。
- Git 历史抽取。

## 模块边界

v1 中本路径刻意保持空实现。它用于把未来 document 和 Concept 工作放在与 code、git 相同的事实抽取器层级下。

## 验证要求

实现 doc 抽取时，必须包含确定性 fixtures、畸形输入测试，以及可选 LLM 路径的 fallback 行为测试。
