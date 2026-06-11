# 模块设计:internal/journal

全量行为日志:单函数模块,一切可观察性的数据底座。

## 职责与边界

- 唯一入口 `Append(root, seat, event string, fields map[string]any) error`:
  向 `.chess/log/journal.jsonl` 追加一行 JSON(自动补 `ts`(UTC RFC3339)、`seat`、`event`),
  写后 fsync。
- **不做**:轮转、检索、格式版本兼容(非目标);调用方失败不阻塞业务
  (各处均为 `_ = journal.Append(...)` 尽力而为)。

## 为什么这么小

日志的消费者是**人与离线分析**,不是程序——所以只需要 append-only JSONL + 字段自由的
map。一个函数、零依赖、无状态,任何席位进程(乃至验证谓词拉起的嵌套席位)都可安全
并发追加:`O_APPEND` 下单次小幅写在 POSIX 上原子,行间不会交错。

## 事件总表(生产方:seat 与 match)

| event | 附加字段 | 时机 |
|---|---|---|
| `seat_started` / `seat_stopped` | `pid` | 席位进程启停 |
| `seat_denied` | `pid, reason(missing_env / missing_keyfile / mismatch)` | 特权席位凭证校验失败,拒绝服务退出 |
| `tool_called` | `tool, args(全量), ok, error_code?, duration_ms` | 每次接口调用(含错误)——"记录模型行为用于后续分析"的素材主体 |
| `match_started` | `match_id, playbook, entry` | 装载 |
| `match_replaced` | `old_match_id` | 替换装载(旧对局让位) |
| `round_entered` | `match_id, round, step` | 进入回合 |
| `round_adjudicated` | `match_id, round, step, complete, reason?, outcome?, target?` | 每次 CheckStepJob 裁决(含未完成) |
| `task_created` | `match_id, task, request` | CreateTask |
| `task_submitted` | `match_id, task, verdict, summary, spec, exit_code 或 is_error, duration_ms, output(截断后)` | SubmitTask(含被作废的 stale 提交,stale 行无 summary) |
| `goal_checked` | `match_id, round, verdict, duration_ms, failure?` | checkmate 谓词的每次执行 |
| `stop_blocked` | `match_id, round, step, blocks` | 停止门控拦截一次(seat 字段为 `hook`) |
| `playbook_added` | `name, file, bytes, steps, has_goal` | AddPlayBook 注册新棋谱 |
| `playbook_noted` | `match_id, playbook, file, step, note, added` | NotePlaybook 追加注记(added=false 为幂等去重命中) |
| `match_finished` / `match_aborted` | `outcome(+checkmate?)` / `abort(steps_exhausted/stop_limit/replaced)` | 终局 |

match 域事件在状态锁内顺带写出(与状态变更同窗);`tool_called` 由 seat 层在
handler 外环写出。

## 消费方式

```sh
tail -f .chess/log/journal.jsonl | jq .                       # 实时
jq -r 'select(.event=="task_submitted") | [.task,.verdict] | @tsv' .chess/log/journal.jsonl
```

`seat_denied` 值得单独告警:它既是配置错误信号,也是冒名拉起特权席位的检测信号
(见 [security.md](../security.md))。
