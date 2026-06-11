# incremental 规格收敛设计草稿

## 状态与边界

- 日期：2026-06-06
- 状态：草稿，等待设计 PR 检视
- 关联：#227，#154
- 范围：`src/cipher2/incremental/`、`src/cipher2/mcp/`、`src/cipher2/storage/` 的临时 overlay view、`src/cipher2/tools/log/`、`src/cipher2/tools/views/`、incremental/config 相关 README。

本设计只处理在线临时增量 overlay 的规格收敛，不实现持久 delta chain、不移动 `snapshots/current`、不新增 HTTP/serve CLI、不引入第三方 watcher。实现阶段仍必须先做 README 搬迁 PR，再按 TDD 修改代码。

## 问题判断

#227 暴露的是规格与运行时不一致，而不是单点代码缺陷。按当前 v1 FACT-only、本地 stdio MCP、临时 overlay 可丢弃的边界，本设计做如下取舍：

- 实现正确性相关收敛：TTL、base-only stale/pending view、运行期 fail-closed 失效、dirty reason 构造路径、warning 观测。
- 本 issue 永久放弃真实 incremental worker pool：`incremental.worker_count` 保持兼容读取和校验，但 v1 不再承诺它改变增量抽取并行度。真实 worker pool 需要另开设计，覆盖 extractor 进程隔离、generation cancel、队列背压和性能门禁。

## 规格决策

1. `overlay_ttl_seconds` 必须生效。overlay 发布后记录 `published_at_monotonic` 和 `last_access_monotonic`；`current_view()`、`notify_file_changed()`、`reconcile_current_sources()`、poll scan 入口都先检查 TTL。超过空闲 TTL 时 drop overlay，查询回到 base view，并写 `incremental.overlay_dropped(status="warning", reason="ttl_expired")`。
2. `worker_count` 不再表示 active incremental worker 数。`incremental.poll_started` 保留兼容字段，并补充 `configured_worker_count` 与 `active_worker_count=1`；README 标注该配置为 v1 保留字段，不影响增量抽取并行度。
3. dirty planning 开始后，coordinator 必须发布 base-only `view_state="pending"`；无法安全构建 overlay 但 base 仍可查时发布 base-only `view_state="stale"`。这两个状态不应用 upsert/tombstone，只通过 MCP structured content、state.json 和 tools/views 告知查询结果来自 base snapshot。
4. 当前 overlay 必须绑定发布时的运行期指纹：`base_snapshot_id`、storage/read-index schema fingerprint、dirty source 的 `compile_command_hash`、dirty source 的 `toolchain_hash`。任一指纹变化时立即 drop overlay，返回 base view，并写 warning 事件；不得继续返回旧 overlay。`IncrementalCoordinator.config` 是启动时快照；长驻 stdio server 不热加载 config，因此本设计不加入 config-change guard。
5. dirty reason 枚举必须有真实构造路径：`content_changed`、`included_header_changed`、`missing`、`compile_command_changed`、`toolchain_changed`。其中 `toolchain_changed` 默认进入 stale 状态并提示全量 `cipher2 rebuild`；除非未来设计证明全仓临时 overlay 可安全分批发布，否则不自动全仓增量抽取。

## 数据结构

只新增进程内状态，不改变 snapshot schema：

```text
OverlayRuntimeGuard:
  overlay_id: str
  base_snapshot_id: str?
  storage_schema_fingerprint: str
  source_compile_command_hashes: dict[source_id, str?]
  source_toolchain_hashes: dict[source_id, str]
  published_at_monotonic: float
  last_access_monotonic: float
```

扩展或复用 `TemporaryOverlay` 的 base-only metadata：

```text
view_state: "stale" | "pending" | "overlay"
stale_source_count: int
pending_task_count: int
```

`view_state!="overlay"` 时 storage view 继续查询 base snapshot，不应用 overlay 数据。

## 接口流程

`current_view()`：

1. 检查 TTL。
2. 检查 runtime guard 指纹。
3. 若 guard 失效，`_drop_overlay(reason, status="warning")` 并返回 base view。
4. 若仍有效，刷新 `last_access_monotonic` 并返回 active view。

`notify_file_changed()` / poll scan：

1. 规范化路径并读取 source inventory。
2. 文件缺失时构造 `DirtySource(reason="missing")`，发布 pending view，构建 tombstone-only overlay。
3. 内容 hash 变化时按现有 source/header fanout 规划 `content_changed` 或 `included_header_changed`。
4. compile command fingerprint 与 inventory `compile_command_hash` 不同时构造 `compile_command_changed`。
5. toolchain fingerprint 与 inventory `toolchain_hash` 不同时写 `dirty_planned(status="warning", reason="toolchain_changed")`，发布 stale view，不发布 overlay。

`reconcile_current_sources()`：

- MCP 启动时继续同步对账，保证首个查询不返回已知 stale base。
- 对已发布 overlay 也执行 runtime guard；base snapshot/schema/toolchain 变化时先 drop 旧 overlay，再重新规划。

## 可观测性

- `incremental.dirty_planned` 使用 `status="warning"` 表达 `toolchain_changed`、`dirty_set_too_large`、无法确定 header fanout、compile command 缺失等 base-only stale 场景。
- `incremental.overlay_dropped` 使用 `status="warning"` 表达 `ttl_expired`、`base_snapshot_changed`、`storage_schema_changed`、`compile_command_changed`、`toolchain_changed`；`stop` 和 `reverted_to_base` 保持 `ok`。
- `incremental.overlay_published.payload` 可增加短 `guard_fingerprint`，不得记录绝对路径、完整 compile command、源码正文或 config dump。
- tools/views 优先读取 `state.json` 的 `state`、`pending_task_count`、`stale_source_count`，不再依赖不可达的 `dirty_planned(status="warning")` 推断分支。

## README 搬迁计划

设计合入后，README 搬迁 PR 至少更新：

- `src/cipher2/incremental/README.md`：写入 5 项决策，特别说明 worker pool 放弃、TTL、stale/pending 和 fail-closed guard，并保留或细化 base snapshot、toolchain、schema 变化时 overlay 必须失效的 MUST。
- `src/cipher2/config/README.md`：标注 `incremental.worker_count` 为 v1 保留字段，仍校验但不改变并行度。
- `src/cipher2/mcp/README.md`：说明 long-running stdio MCP 每次查询都会经 `current_view()` 执行 overlay 失效检测。
- `src/cipher2/tools/log/README.md` 与 `src/cipher2/tools/views/README.md`：更新 warning status 和 stale/pending 呈现语义。
- `README.md`、`docs/user-guide.md`、`docs/maintenance-guide.md`、`tests/README.md`：递归同步用户可见行为和门禁。

## TDD 与门禁

- TTL：overlay 发布后空闲超过 `overlay_ttl_seconds`，下一次 `current_view()` 回 base，并写 warning drop 事件。
- stale/pending：dirty planning 到抽取完成之间的并发查询返回 base result，structured content 带 `view_state="pending"`；warning dirty 场景返回 `view_state="stale"`。
- fail-closed：base snapshot、read-index schema、dirty source compile command、toolchain hash 任一变化，旧 overlay 不再参与查询。
- dirty reasons：覆盖 `missing` tombstone-only overlay、`compile_command_changed` 重抽、`toolchain_changed` stale warning、header fanout 与普通内容变化。
- 观测：`dirty_planned` 和 `overlay_dropped` 的 warning 分支在 log summary 和 tools/views 中可见。
- 门禁：实现 PR 至少运行 `PYTHONPATH=src python3 -m unittest tests.test_incremental_overlay_view tests.test_incremental_mcp_view_state tests.test_incremental_observability`，并按影响范围运行 `PYTHONPATH=src python3 scripts/incremental_performance_gate.py`。
