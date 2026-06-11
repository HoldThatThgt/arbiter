# 检索质量复测报告

## 状态

本文件是 #95 的报告落点。当前 PR 落地了可复现 harness 和报告生成能力，但未在仓库内伪造 10 库快照或弱模型 API 结果。

真实复测需由维护者准备 10 个目标仓库 snapshot 与弱模型 adapter 环境后运行：

```bash
PYTHONPATH=src:. python3 -m benchmarks.retrieval.run --manifest /path/to/retest-manifest.json --mode all --output docs/eval-report-2026-05-29-retest.md
```

## 指标口径

| 指标 | 含义 |
|---|---|
| `recover@preview` | 只使用 MCP `search` + `detail` 响应中模型可见的答案名称。 |
| `recover@full` | 使用 store 中已存在的关系端点，表示当前覆盖天花板。 |
| `preview_gap` | `recover@full - recover@preview`，表示呈现层剩余损失。 |
| `ceiling_delta` | 当前 `recover@full` 相对修复前基线的变化，表示覆盖层抬升。 |
| `acc_B` | 弱模型在 grep-only 条件下的平均得分。 |
| `acc_C` | 弱模型在 grep + cipher 条件下的平均得分。 |
| `delta` | `acc_C - acc_B`。 |
| `rescue` | `delta / (1 - acc_B)`，当 `acc_B=1` 时为 `0`。 |

## 结果

尚未运行真实 10 库 + 弱模型 A/B。运行后由 harness 覆盖本节。
