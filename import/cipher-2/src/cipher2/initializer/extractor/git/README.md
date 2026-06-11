# initializer/extractor/git

## 路径职责

本包预留给未来的 git 事实抽取。

## 应放入这里的内容

- 未来 commit、author、hunk 和路径变更 facts。
- 未来变更行与代码 facts 的映射。

## 不应放入这里的内容

- v1 运行时代码。
- 代码抽取。
- 文档或 Concept 抽取。

## 模块边界

Git 支持不属于 v1。加入时，本包只能产出 facts/relatives，不应创建上层查询产物。

## 验证要求

未来测试应覆盖 rename 处理、binary files、shallow repositories、detached HEAD 和行号范围重叠边界。
