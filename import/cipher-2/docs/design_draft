# in-process libclang per-file AST timeout 设计草稿

- 状态：设计草稿；未搬迁到 README；未实现。
- 关联 issue：#224。
- 记录日期：2026-06-06。
- 范围：恢复生产 libclang target AST 单文件 timeout，使超大或病态 C 文件被跳过并记录 `clang_ast_failed`，而不是无限阻塞 init/rebuild。

## 模块定位

- `src/cipher2/initializer/extractor/code/streaming.py`：替换当前 `ProcessPoolExecutor` + `future.result()` 的生产文件抽取调度，改为 coordinator 可 kill/restart 的 per-file worker 生命周期管理。
- `src/cipher2/initializer/extractor/code/ast_backend.py`：保持 in-process ctypes libclang parse，不在 C 路径引入 JSON dump、subprocess mapper 或 lightweight parser fallback。
- `src/cipher2/initializer/extractor/code/models.py`：如需扩展，只增加内部 worker 状态/result 数据结构；不改变 `CodeFact`、`FactRelative`、snapshot 或 MCP schema。
- `tools/log` / `tools/views`：沿用文件级 warning 呈现，必要时补充 worker timeout/restart 统计。

README 基准保持不变：`cipher-2` 是 FACT-only、本地 stdio MCP；C 抽取固定使用类型驱动 libclang AST；worker 只写 `.cipher/run/initializer-mapreduce/<run_id>/` run-local map 段；单文件 AST failed/partial 是可观测 warning，不降级为其他解析器。

## 问题判断

`_ast_command_timeout_seconds()` 已有 120 秒基础、每 1 MiB 增加 30 秒、最高 600 秒的公式，但当前只用于测试 JSON subprocess backend。生产路径在 `_load_ast_for_path()` 内通过 ctypes 调用 `clang_parseTranslationUnit`，该调用无法协作式中断；只给 `Future.result(timeout=...)` 加 timeout 会留下仍在阻塞的 worker，且 executor shutdown 仍可能等待该进程。

因此实现不能依赖 `ProcessPoolExecutor` 的 future timeout，也不能在 coordinator 进程直接解析单文件。需要把每个正在解析的文件放在可强制终止的 worker 进程中，并由 coordinator 在超时后映射为文件级 warning。

## 规格约束

- timeout 只覆盖 target source AST parse + traverse + mapper 的单文件 worker 窗口；不覆盖 toolchain capability probe、compile database 读取、storage 写入或 direct_call resolver。
- timeout 秒数由 `_ast_command_timeout_seconds(source)` 计算：基础 120 秒，每 1 MiB 增加 30 秒，最高 600 秒；不新增 CLI 参数或持久配置项。
- timeout 后该 source 必须生成 `clang_ast_failed` warning，`diagnostic_kind="timeout"`、`reason="timeout"`、details/payload 含 `timeout_seconds`；该文件不进入 `source_inventory`。
- timeout 文件的 map segment 不得进入 reducer 或 relative merge；若 worker 被 kill 时留下半写 segment，coordinator 必须删除或忽略该 source 的 segment。
- timeout 不取消其他 source；worker 被 kill 后，coordinator 为后续 source 启动 replacement worker。
- timeout worker 不发布 header cache entry，不更新 worker-local relative dedup 状态到 coordinator。
- `worker_count=1` 仍保持一次只处理一个 source 的串行调度语义，但生产 target AST 也必须运行在可 kill 的 child worker 中；否则无法满足 timeout 规格。
- 不使用私有 `ProcessPoolExecutor` 进程表或 Python 3.14+ 专属 API；实现只依赖 Python 3.9 标准库。

## 数据结构

建议新增内部调度结构：

```text
ManagedFileWorker
  worker_id: int
  generation: int
  process: multiprocessing.Process
  task_queue: multiprocessing.Queue
  result_queue: multiprocessing.Queue
  active_item: _FileWorkItem | None
  active_started: float | None
  active_deadline: float | None
  active_timeout_seconds: int | None

ManagedFileWorkerResult
  generation: int
  seq: int
  outcome: _FileWorkOutcome | None
  error: bounded worker init/crash summary | None
```

`_FileWorkItem` 可保持现状，timeout 由 coordinator 计算并作为调度 deadline 保存；也可增加内部 `timeout_seconds` 字段供测试断言。worker 进程继续复用 `_initialize_process_file_worker()`、`_run_file_work_item_in_process()`、worker-local header cache 和 relative deduper。

## 对外接口

不新增 CLI flag、配置项、MCP tool、snapshot 字段或 read index schema。用户可见变化仅为生产 libclang path 在单文件超时时返回成功 init/rebuild 加 warning，CLI JSON `warnings` 与 log payload 能看到相同的 source、`clang_ast_failed`、`timeout_seconds`。

## 并发控制

- coordinator 启动最多 `extractor.worker_count` 个 managed worker；每个 worker 同时只处理一个 `_FileWorkItem`。
- deadline 从 coordinator 把 item 发给 idle worker 后开始计算，避免排队等待消耗单文件 timeout。
- coordinator 轮询 result queue 和 deadline；正常 result 先到则调用既有 `_merge_file_outcome()`。
- deadline 先到则先终止该 worker：优先 `terminate()`，短暂 grace 后仍存活则在支持的平台调用 `kill()`；不得等待无界 shutdown。
- kill 完成后，coordinator 生成 synthetic `_FileWorkOutcome(error_code="clang_ast_failed", diagnostic_kind="timeout", diagnostic_reason="timeout", diagnostic_details={"timeout_seconds": N})` 并按既有 warning 路径合并。
- worker crash 且有 active item 时映射为 `clang_ast_failed` + `reason="worker_crash"`；idle worker crash 只重启，不产生文件 warning。
- replacement worker 使用新 generation；旧 generation 的迟到 result 必须丢弃，避免 timeout 后的 stale outcome 污染 reducer。
- 被 kill worker 的 process-local header cache 和 dedup 表随进程丢弃；这是性能损失，不改变 snapshot identity。跨 worker/cache miss 仍由 reducer、external merge 和 direct_call resolver 保证确定性。

## 递归文档更新

设计 PR 合入后，README 搬迁 PR 至少更新：

- `README.md`、`docs/user-guide.md`：说明生产 libclang target AST timeout 的文件级 warning 行为。
- `src/cipher2/initializer/README.md`、`src/cipher2/initializer/extractor/README.md`、`src/cipher2/initializer/extractor/code/README.md`：写明 managed worker kill/restart、`worker_count=1` 的 killable child 语义、timeout details 和 segment/header-cache 边界。
- `src/cipher2/tools/log/README.md`、`src/cipher2/tools/views/README.md`：补充 timeout/restart 统计和 warning 呈现。
- `tests/README.md`：补充生产 worker timeout 覆盖，避免只测 JSON backend。

README 搬迁 PR 合入前不得修改运行时代码或测试。

## 可观测性

- `extractor.code.file` timeout warning 写 `status="warning"`、`error_code="clang_ast_failed"`、`diagnostic_kind="timeout"`、`diagnostic_reason="timeout"`、`timeout_seconds`、`source_kind` 和 `profile`。
- `InitSummary.errors` 与 CLI JSON `warnings` 暴露同一有界 details，不记录完整 command、diagnostic 文本、源码正文或绝对 target path。
- `extractor.code.worker_pool` 可新增内部计数：`worker_timeout_count`、`worker_restart_count`、`worker_crash_count`。这些计数用于 liveness/恢复诊断；timeout 文件本身仍通过文件级 warning 呈现。

## 可观测用例看护

- `worker_count=1` 下单个 source 卡住时，超过公式 timeout 后 init 继续并产生 timeout warning。
- `worker_count=2` 下一个 source 卡住、另一个 source 正常完成时，正常 source 的 facts/relatives/source inventory 不受影响。
- timeout 后 replacement worker 能继续处理后续 source；timeout source 不发布 header cache entry，也不登记 map segment。
- worker crash 与 timeout 使用不同 `reason`，但都不泄漏 traceback、绝对路径或源码正文。

## 测试与门禁计划

实现 PR 至少覆盖：

- `_ast_command_timeout_seconds()` 的 120 秒默认、按 MiB 增长和 600 秒上限。
- managed worker 单元测试：fake worker hang 后被 kill，返回 timeout warning；fake worker crash 映射为 `worker_crash`；replacement worker 继续处理下一文件。
- 生产调度路径测试：不通过 JSON subprocess backend，直接验证 `worker_count=1` 和 `worker_count>1` 的 timeout 合并语义。
- map segment 边界：timeout 文件半写 segment 不进入 reducer/merge，成功文件仍可产出 snapshot。
- log/summary：`InitSummary.errors`、CLI JSON warnings、`extractor.code.file` payload 均含 `diagnostic_kind="timeout"` 和 `timeout_seconds`。

实现 PR 建议先运行目标测试，再运行：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src python3 scripts/initializer_performance_gate.py
PYTHONPATH=src python3 scripts/clang_extractor_performance_gate.py
```

本设计阶段不运行实现门禁，不修改运行时代码或测试。
