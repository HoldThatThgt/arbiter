# benchmarks

## 路径职责

本目录存放不进入默认 CI 硬门禁的手动评测工具。当前只包含检索质量评估 harness，用于在已构建的 `.cipher/` snapshot 上复测 `search` + `detail` 的可还原率和弱模型 A/B 回归。

## 可用工具

| 路径 | 作用 |
|---|---|
| `retrieval/` | 离线、确定性的检索质量评估 harness；probe 模式不访问网络、不调用模型、不运行 `cipher2 init` / `cipher2 rebuild`，A/B 模式只通过显式配置的外部 adapter 调用模型。 |
