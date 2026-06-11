# Chess 用户手册

从零到跑通第一局,每一步都可以整段复制粘贴。

前置条件:macOS 或 Linux、Go 1.22+(仅构建时需要)、Claude Code。

---

## 1. 安装

```sh
git clone git@github.com:HoldThatThgt/chess.git
cd chess
go build -o chess ./cmd/chess
sudo mv chess /usr/local/bin/      # 或移动到任意 PATH 目录
chess 2>&1 | head -1               # 应输出: usage: chess init | serve <seat>
```

> 依赖已全部 vendor 入库,`go build` 自动离线完成,**不需要访问 proxy.golang.org
> 或任何镜像**——内网/内源机器把仓库拷过去即可构建。构建产物是单一静态二进制,
> 运行期零依赖,也可以在一台机器构建后直接分发给同 OS/架构的其他机器。

## 2. 部署到你的仓库

```sh
cd /path/to/your-repo
chess init
```

init 是幂等的(重复执行安全),它会:

| 产物 | 说明 |
|---|---|
| `.chess/playbook/` | 棋谱目录(对 Claude Code 文件工具封禁,你可以自由编辑) |
| `.chess/FORMAT.md` | 棋谱格式速查(给人看) |
| `.chess/run/seat.key` | 席位凭证(本机生成,0600) |
| `.mcp.json` | 注册棋手席位服务器(合并写入,不动你的既有配置) |
| `.claude/agents/chess-curator.md` | 领谱 subagent(已注入凭证,自动加入 .gitignore) |
| `.claude/skills/chessplay/SKILL.md` | 棋手操作规程 skill |
| `.claude/settings.json` | 追加 4 条路径 deny 规则(合并写入) |

## 3. 放入棋谱

棋谱是带 frontmatter 的 markdown,放在 `.chess/playbook/` 下。先放一个能直接用的示例
(修复构建失败的两步流程,可按需改文字):

```sh
cat > .chess/playbook/hotfix-verify.md <<'EOF'
---
name: hotfix-verify
description: 修复构建失败并验证回归的标准流程。适用于 CI 红灯、编译报错场景。
max_steps: 32
---

[SetGoal]
shell: make test

[STEP] diagnose
[StepJob]
定位构建失败的直接原因。阅读最近一次构建日志,确认失败的目标与首个报错,
给出修复方向。不要修改任何代码。
[CheckList]
- 产出失败根因结论与证据文件路径
- 确认失败可在本地复现
[Branch]
success: fix
failure: diagnose

[STEP] fix
[StepJob]
按上一步结论实施最小修复,只允许修改与根因直接相关的文件。
[CheckList]
- 完成修复且构建通过
- 现有测试全部通过
[Branch]
success: END
failure: diagnose
EOF
```

两个关键开关:`max_steps` 是回合预算(默认 256,耗尽强制终局),`[SetGoal]` 是
**checkmate 谓词**——通过即整局胜利;声明了 goal 之后,模型在将死或预算耗尽之前
无法自行停止(Stop 门控会把它拦回棋局)。格式规格与编写建议见
[playbook-format.md](playbook-format.md)。

**懒人路线**:不想手写就让模型来——会话里输入 `/playbook-create 描述你的流程`,
模型会访谈缺失的细节、起草棋谱并通过 AddPlayBook 注册(它没有棋谱目录的文件权限,
注册是唯一通道,且只能新建、不能覆盖既有棋谱)。

## 4. 创建执行席位(必需,整段复制)

执行席位是干脏活的 subagent,由你提供。下面这段会自动填入二进制路径与本机凭证:

```sh
CHESS_BIN="$(command -v chess)"
SEAT_KEY="$(cat .chess/run/seat.key)"
cat > .claude/agents/chess-executor.md <<EOF
---
name: chess-executor
description: 执行 chess 任务并提交可验证结果
tools: Bash, Read, Write, Edit, Glob, Grep, mcp__chess-executor__SubmitTask, mcp__chess-executor__ReviewTask
mcpServers:
  chess-executor:
    type: stdio
    command: ${CHESS_BIN}
    args: [serve, executor]
    env:
      CHESS_SEAT_KEY: ${SEAT_KEY}
---

你是任务执行者。完成提示中的任务后,必须调用 SubmitTask:
task_id 取提示中的编号,summary 一句话概括结果(进全局任务清单,
供棋手通览与复盘),report 写明做了什么与证据,result 填能独立验证
任务完成的谓词——shell 命令(退出码 0 即通过)或 mcp 调用
(server/tool/arguments,应答非错误即通过)。验证耗时长或输出大时,
可在 result 中附 timeout_s(默认 600)/ output_lines(默认 256)。
提交后可用 ReviewTask 查看判定;失败可修复后重交。
EOF
grep -qxF '.claude/agents/chess-executor.md' .gitignore || echo '.claude/agents/chess-executor.md' >> .gitignore
```

> 凭证是本机的,该文件不要提交(上面最后一行已帮你 gitignore)。
> agent 的指令部分可以随意加强(比如你的构建/测试习惯),但 frontmatter 的
> `mcpServers` 块与"必须调用 SubmitTask"的收尾要求不能少。

## 5. 减少权限弹窗(可选)

Claude Code 首次调用每个工具会弹确认。提前放行 Chess 的全部接口:

```sh
python3 - <<'EOF'
import json, os
path = ".claude/settings.local.json"
cfg = json.load(open(path)) if os.path.exists(path) else {}
allow = cfg.setdefault("permissions", {}).setdefault("allow", [])
for rule in [
    "mcp__chess__ShowStepJob", "mcp__chess__CreateTask",
    "mcp__chess__CheckStepJob", "mcp__chess__ListTask",
    "mcp__chess__ReviewTask", "mcp__chess__NotePlaybook",
    "mcp__chess__AddPlayBook",
    "mcp__chess-curator__ReadPlayBook", "mcp__chess-curator__LoadPlayBook",
    "mcp__chess-curator__ReviewTask",
    "mcp__chess-executor__SubmitTask", "mcp__chess-executor__ReviewTask",
]:
    if rule not in allow: allow.append(rule)
json.dump(cfg, open(path, "w"), indent=2, ensure_ascii=False)
print("ok:", path)
EOF
```

## 6. 开局

```sh
claude
```

会话里直接说,或显式用 skill:

```text
/chessplay 按流程修掉 CI 红灯
```

之后无需任何操作:棋手召唤 curator 选谱装载 → 按步拆任务派 executor →
裁判核验每个任务的验证谓词 → 裁决推进/分支 → 终局汇报。

对局进行中模型**无法自行停止**(Stop 门控拦截,直到 checkmate、走到 END 或预算
耗尽);你的人工中断(Esc/Ctrl+C)不受影响,随时可打断。

## 7. 旁观与复盘

```sh
watch -n1 cat .chess/status.json                 # 实时局面(当前步/任务/历史回合)
tail -f .chess/log/journal.jsonl                 # 全量行为日志(JSONL)
tail -f .chess/log/journal.jsonl | jq .          # 有 jq 的话更好看
```

`status.json` 只含当前与历史,永不泄露未来步骤;`journal.jsonl` 记录每次接口调用、
每个谓词的命令与输出尾部、每次裁决与分支,留作事后分析。

## 8. 常见问题

| 现象 | 原因与处理 |
|---|---|
| curator/executor 启动失败,journal 出现 `seat_denied` | 凭证不一致:executor 文件里的 `CHESS_SEAT_KEY` 与 `.chess/run/seat.key` 不符(常见于换机、误删 run 目录)。重跑 `chess init`,再重跑第 4 步重建 executor 文件 |
| 棋手报"chess-executor 不存在" | 第 4 步没做或文件名不对。执行席位必须命名为 `chess-executor` |
| 对局卡住,CheckStepJob 一直 `open_tasks` | 有任务从未被提交:executor 没调 SubmitTask(看它的最终消息),或被权限弹窗拦住(做第 5 步) |
| `playbook_invalid` | 棋谱不合文法,错误里有逐条 file/line/code,对照 [playbook-format.md](playbook-format.md) 修 |
| `state_busy` | 并发锁竞争(5s 超时),偶发重试即可;若持续,检查是否有僵死的 verify 子进程 |
| 对局 `aborted / steps_exhausted` | 回合预算耗尽仍未将死:加大该棋谱的 `max_steps`,或检查 goal 是否现实、分支是否在空转 |
| 对局 `aborted / stop_limit` | 模型在同一回合被停止门控拦了 32 次仍无进展(卡死保护):看 journal 里它卡在哪一步,通常是 executor 缺失或任务无法验证 |
| 模型一直不停、在自言自语 | 预期行为:对局未终局前 Stop 门控不放行。不想等就人工中断,或让 curator 换棋谱(替换即终止旧局) |
| 想中途换棋谱 | 直接让 curator 重新装载:旧对局被替换并记入日志 |
| 模型读不了 `.claude/agents/chess-executor.md` | 预期行为:该文件含凭证,已被 deny 规则封禁。要改它就用编辑器手改 |

## 9. 换机 / 卸载

**换机**:凭证与二进制路径是本机的——在新机器上重跑第 1、2、4 步即可(棋谱跟随仓库,无需重做)。

**卸载**:

```sh
rm -rf .chess .claude/agents/chess-curator.md .claude/agents/chess-executor.md .claude/skills/chessplay
# 再手动移除 .mcp.json 的 mcpServers.chess 条目、.claude/settings.json 中 4 条 chess 相关 deny 规则
```
