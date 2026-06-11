# retrieval benchmark

## 路径职责

本目录存放检索质量评估 harness。它只读取已经初始化完成并锁定的目标仓库 `.cipher/snapshots/current`，复现模型经 MCP `search` + `detail` 能看到的 preview，并与 store 直读的 full 天花板对比。

## 运行命令

公开 `python -m` 入口只有两个：

```bash
PYTHONPATH=src:. python3 -m benchmarks.retrieval.retrieval_probe /path/to/repo --manifest benchmarks/retrieval/fixtures/smoke.json
PYTHONPATH=src:. python3 -m benchmarks.retrieval.run --manifest benchmarks/retrieval/fixtures/smoke.json --output /tmp/cipher2-retrieval-smoke
```

`ast_gold.py`、`coverage_pool.py`、`genq.py`、`score.py` 和 `analyze.py` 仅供 import，不作为独立命令入口。

## 口径

- `recover@preview`：in-process `cipher2.mcp.open_mcp_server(repo).search/detail` 在指定 `--budget` 下返回的可见答案比例。
- `recover@full`：storage / coverage pool 直读 store 得到的完整关系天花板，不走 MCP `large`。
- `bound_loss`：`recover@full - recover@preview`，与复测设计中的 `preview_gap` 同义。
- `preview_partial`：`recover@full` 已覆盖但 bounded preview 只召回部分答案的既有根因；高扇入 `FIELD_ACC` 字段读写者被每桶预算截断时使用该分类，不新增 `missing_fact` / `missing_relative` 或 selector-quality 平行分类。
- snapshot 前置条件：manifest 的 `snapshot_id` 必须等于 `.cipher/snapshots/current` 内容，`snapshot_path` 必须存在；否则该仓库标记为 `snapshot_mismatch` 或 `snapshot_missing`，不会自动 init/rebuild。

## Manifest

最小 JSON 示例：

```json
{
  "clang_executable": "/usr/bin/clang-16",
  "seed": 7,
  "dimensions": ["CALLERS"],
  "case_limit": 10,
  "repositories": [
    {
      "name": "smoke",
      "repo_root": "/path/to/repo",
      "snapshot_id": "sha256-...",
      "snapshot_path": ".cipher/snapshots/sha256-...",
      "clang16_version": "LLVM Clang 16.0.6",
      "cases": [
        {
          "case_id": "smoke-callers",
          "dimension": "CALLERS",
          "query": "add_var",
          "target_fact_id": "fact:function:add_var",
          "gold_answers": ["div_mod_var"]
        }
      ]
    }
  ]
}
```

## 输出

`run` 输出 `run_summary.json` 和 `report.md`。JSON 中每个 metric 稳定包含 `library`、`dimension`、`case_count`、`recover_preview`、`recover_full`、`bound_loss` 和 `skip_reason`；`coverage` 段记录 `covered_count`、`gold_count` 和 `precision`。

## Retest and Weak Model A/B

同一 `run` 入口也接受复测 manifest。复测 manifest 使用 `libraries` 字段而非 `repositories` 字段，直接指向已初始化的目标仓库，并可选配置 `model_plan` 外部 adapter：

```json
{
  "seed": 7,
  "clang16_gold_version": "LLVM Clang 16.0.6",
  "baselines": [
    {"library": "postgres", "dimension": "CALLERS", "preview_before": 0.16, "full_before": 0.83}
  ],
  "libraries": [
    {
      "name": "postgres",
      "repo": "/path/to/repo",
      "snapshot_id": "current",
      "cases": [
        {
          "case_id": "pg-callers-001",
          "dimension": "CALLERS",
          "query": "add_var",
          "question": "Who calls add_var?",
          "target_fact_id": "fact:function:add_var",
          "gold_answers": ["div_mod_var"],
          "grep_context": ["grep baseline context"]
        }
      ]
    }
  ]
}
```

复测报告使用 `recover@preview`、`recover@full`、`preview_gap = recover@full - recover@preview` 和 `ceiling_delta = recover@full - baseline.full_before`。`--mode ab` 或 `--mode all` 会启用弱模型 A/B；adapter 从 stdin 接收 JSON request，必须向 stdout 返回：

```json
{"case_id": "pg-callers-001", "condition": "grep_cipher", "answer_names": ["div_mod_var"], "raw_answer": "div_mod_var"}
```

`condition=grep` 表示只给 grep 上下文，`condition=grep_cipher` 表示同时给 cipher preview/full context。`required_env` 缺失时该 A/B 段标记为 skip；普通 CI 只覆盖小型 fixture，真实 10 库和真实弱模型 A/B 是人工门禁。

## Multi-hop Probe

Issue #127 的 T4/T5 类弱模型 probe 用来验证有界传递查询是否减少手动链式 BFS：

- T4 类问题：给定锚点函数，要求列出 2 跳内全部 callees 或 callers。`grep_cipher` 条件必须优先使用 `search("callees:<function> depth:2")` 或 `search("callers:<function> depth:2")`，并检查 `complete` / `budget_exhausted`，而不是让模型连续调用一跳 `callees:`。
- T5 类问题：给定起点和目标函数，询问是否间接调用。`grep_cipher` 条件必须优先使用 `search("reachable:<start>-><target>")`，命中时按返回 `path` 作答；`complete=false` 时只能声明 bounded depth 或预算内未证明可达，不得回答全局不可达。
- 报告应记录每题 tool call 数、是否触发 `needs_refinement`、是否触发 `too_broad` / `budget_exhausted`、`reachable` 正确性和 closure 召回率。验收目标是弱模型不再需要 25 到 30 次手动多跳查询即可完成同类问题。
