package match

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
)

// 子代理停止门控(SubagentStop hook):被派发的 task 在 SubmitTask 之前不
// 准收工 — 没有 typed result 的派发对裁判不存在。归属:从子代理 transcript
// 的首条用户消息(= 派发 prompt 原文)提取候选 task id,再以对局状态裁定
// 哪些真实且仍 open — 规程要求 "task id: <id>" 标注行(#84),但执行模型
// 常自由转述("dispatched for task T2"),所以宽提取、严校验。姿态与 Stop
// 门控一致:解析失败 fail-open,命中 fail-closed,封顶放行交还重派权。

var (
	labeledTaskID = regexp.MustCompile(`(?mi)^\s*task id:\s*([A-Za-z0-9_-]+)`)
	bareTaskID    = regexp.MustCompile(`\b(T\d+)\b`)
)

// ResolveSubagentTranscript 把 hook 输入(主会话 transcript_path + agent_id)
// 解析成子代理自身的 transcript 路径:<session-dir>/subagents/agent-<id>.jsonl
// (2.1.173 实测布局)。推导失败或文件不存在时退回原路径 —— 若宿主某天直接
// 传子代理路径,原样可用;传主路径则首条用户消息没有在局 task id,门控放行。
func ResolveSubagentTranscript(transcriptPath, agentID string) string {
	if transcriptPath == "" {
		return ""
	}
	if agentID != "" && strings.HasSuffix(transcriptPath, ".jsonl") {
		derived := filepath.Join(strings.TrimSuffix(transcriptPath, ".jsonl"), "subagents", "agent-"+agentID+".jsonl")
		if _, err := os.Stat(derived); err == nil {
			return derived
		}
	}
	return transcriptPath
}

// ExtractDispatchTaskIDs 从 transcript(JSONL)首条用户消息提取候选派发
// task id:标注行捕获 + 裸 T<n> 词元,去重保序。读取/解析问题返回 nil
// (fail-open);真伪由对局状态裁定,这里只负责候选。
func ExtractDispatchTaskIDs(transcriptPath string) []string {
	if transcriptPath == "" {
		return nil
	}
	f, err := os.Open(transcriptPath)
	if err != nil {
		return nil
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
	for scanner.Scan() {
		var entry struct {
			Type    string `json:"type"`
			Message struct {
				Role    string          `json:"role"`
				Content json.RawMessage `json:"content"`
			} `json:"message"`
		}
		if err := json.Unmarshal(scanner.Bytes(), &entry); err != nil {
			continue
		}
		if entry.Type != "user" || entry.Message.Role != "user" {
			continue
		}
		text := flattenContent(entry.Message.Content)
		if text == "" {
			return nil
		}
		var ids []string
		seen := map[string]bool{}
		for _, m := range labeledTaskID.FindAllStringSubmatch(text, -1) {
			if !seen[m[1]] {
				seen[m[1]] = true
				ids = append(ids, m[1])
			}
		}
		for _, m := range bareTaskID.FindAllStringSubmatch(text, -1) {
			if !seen[m[1]] {
				seen[m[1]] = true
				ids = append(ids, m[1])
			}
		}
		return ids // 首条用户消息即派发 prompt;后续消息里的 id 是回显,不算
	}
	return nil
}

// flattenContent 兼容 content 的两种形态:纯字符串与 content-block 数组。
func flattenContent(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		return s
	}
	var blocks []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if err := json.Unmarshal(raw, &blocks); err != nil {
		return ""
	}
	var b strings.Builder
	for _, blk := range blocks {
		if blk.Type == "text" {
			b.WriteString(blk.Text)
			b.WriteString("\n")
		}
	}
	return b.String()
}

// SubagentStopGate 对一次子代理停止做出门控决定:候选 id 中存在当前回合
// 仍 open 的任务 → 拒绝(逐一点名 + 规程提示);其余情况(无对局、全部
// 已交、id 不在局、拦截到顶)放行。
func (s *Store) SubagentStopGate(taskIDs []string) (StopDecision, error) {
	if len(taskIDs) == 0 {
		return StopDecision{Allow: true}, nil
	}
	out, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Status != StatusActive || m.Current == nil {
			return nil, StopDecision{Allow: true}, nil
		}
		var open []string
		for _, id := range taskIDs {
			if idx, ok := findCurrentTask(m, id); ok && m.Current.Tasks[idx].Status == TaskOpen {
				open = append(open, id)
			}
		}
		if len(open) == 0 {
			return nil, StopDecision{Allow: true}, nil
		}
		if m.SubagentBlocks == nil {
			m.SubagentBlocks = map[string]int{}
		}
		key := open[0]
		m.SubagentBlocks[key]++
		if m.SubagentBlocks[key] > playbook.SubagentBlockCap {
			s.append("subagent_stop_cap", map[string]any{"match_id": m.ID, "round": m.Current.Seq, "task_id": key, "blocks": m.SubagentBlocks[key]})
			return m, StopDecision{Allow: true}, nil
		}
		reason := fmt.Sprintf(
			"Task %s is dispatched to you and has no submitted result — a dispatch without SubmitTask does not exist for the referee, no matter how good your prose is. ReviewTask {\"task_id\":\"%s\"} to re-read the request, finish the work, pre-verify the exact predicate, then SubmitTask with a typed result. If you are genuinely blocked, SubmitTask a failing result with the blocker in the report.",
			strings.Join(open, ", "), open[0])
		s.append("subagent_stop_blocked", map[string]any{"match_id": m.ID, "round": m.Current.Seq, "task_id": key, "blocks": m.SubagentBlocks[key]})
		return m, StopDecision{Allow: false, Reason: reason}, nil
	})
	if err != nil {
		return StopDecision{}, err
	}
	return out.(StopDecision), nil
}
