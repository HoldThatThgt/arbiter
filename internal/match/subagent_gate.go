package match

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
)

// 子代理停止门控(SubagentStop hook):执行席子代理在当前回合仍有未提交的
// 任务、且它自己从未调用过 SubmitTask 时,不准收工 —— 没有 typed result 的
// 派发对裁判不存在。判定全程走结构化:对局状态(open 任务计数)+ 子代理
// transcript 的结构化工具调用记录(逐行 JSON,按工具名精确比对),不对自由
// 文本做正则/子串匹配。姿态与 Stop 门控一致:读不到证据 fail-open,命中
// fail-closed,封顶放行交还重派权。

// submitTaskToolName 是执行席 SubmitTask 在子代理 transcript 里的结构化工具名。
const submitTaskToolName = "mcp__arbiter-executor__SubmitTask"

// ResolveSubagentTranscript 把 hook 输入(主会话 transcript_path + agent_id)
// 解析成子代理自身 transcript 路径:<session-dir>/subagents/agent-<id>.jsonl。
// 推导失败或文件不存在时退回原路径。纯路径拼接,不涉运行逻辑判定。
func ResolveSubagentTranscript(transcriptPath, agentID string) string {
	if transcriptPath == "" {
		return ""
	}
	const ext = ".jsonl"
	if agentID != "" && strings.HasSuffix(transcriptPath, ext) {
		base := transcriptPath[:len(transcriptPath)-len(ext)]
		derived := filepath.Join(base, "subagents", "agent-"+agentID+ext)
		if _, err := os.Stat(derived); err == nil {
			return derived
		}
	}
	return transcriptPath
}

// SubagentSubmitted 结构化判断子代理是否调用过 SubmitTask:逐行解析 transcript
// (JSONL),在 assistant 消息的 content 块里按结构化 type=="tool_use" 且
// name==submitTaskToolName 精确匹配。读取/解析失败返回 false(门控侧据此与
// open 任务结合决定放行与否)。无正则、无自由文本扫描。
func SubagentSubmitted(transcriptPath string) bool {
	if transcriptPath == "" {
		return false
	}
	f, err := os.Open(transcriptPath)
	if err != nil {
		return false
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
	for scanner.Scan() {
		var entry struct {
			Type    string `json:"type"`
			Message struct {
				Content json.RawMessage `json:"content"`
			} `json:"message"`
		}
		if json.Unmarshal(scanner.Bytes(), &entry) != nil || entry.Type != "assistant" {
			continue
		}
		var blocks []struct {
			Type string `json:"type"`
			Name string `json:"name"`
		}
		if json.Unmarshal(entry.Message.Content, &blocks) != nil {
			continue
		}
		for _, b := range blocks {
			if b.Type == "tool_use" && b.Name == submitTaskToolName {
				return true
			}
		}
	}
	return false
}

// SubagentStopGate:submitted=true(子代理提交过)直接放行;否则当前回合若仍
// 有 open 任务,拒绝其停止(它把派发的任务丢在半空)。无对局/无 open 任务/
// 封顶均放行。计数随回合清零(见 round 进入处)。
func (s *Store) SubagentStopGate(submitted bool) (StopDecision, error) {
	if submitted {
		return StopDecision{Allow: true}, nil
	}
	out, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Status != StatusActive || m.Current == nil {
			return nil, StopDecision{Allow: true}, nil
		}
		open := 0
		for _, task := range m.Current.Tasks {
			if task.Status == TaskOpen {
				open++
			}
		}
		if open == 0 {
			return nil, StopDecision{Allow: true}, nil
		}
		m.SubagentBlocks++
		if m.SubagentBlocks > playbook.SubagentBlockCap {
			s.append("subagent_stop_cap", map[string]any{"match_id": m.ID, "round": m.Current.Seq, "open": open, "blocks": m.SubagentBlocks})
			return m, StopDecision{Allow: true}, nil
		}
		reason := fmt.Sprintf(
			"This round has %d task(s) still awaiting a submitted result and you have not called SubmitTask — a dispatch without SubmitTask does not exist for the referee, no matter how good your prose is. ReviewTask your task, finish it, pre-verify the exact predicate, then SubmitTask a typed result. If you are genuinely blocked, SubmitTask a failing result with the blocker in the report.",
			open)
		s.append("subagent_stop_blocked", map[string]any{"match_id": m.ID, "round": m.Current.Seq, "open": open, "blocks": m.SubagentBlocks})
		return m, StopDecision{Allow: false, Reason: reason}, nil
	})
	if err != nil {
		return StopDecision{}, err
	}
	return out.(StopDecision), nil
}
