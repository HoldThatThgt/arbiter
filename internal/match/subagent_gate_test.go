package match

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
	"github.com/HoldThatThgt/arbiter/internal/verify"
)

func TestSubagentStopGate(t *testing.T) {
	root := repoWithBook(t, "flow.md", twoStepBook)
	store := New(root, "test")

	// 无对局:放行(无论是否提交)。
	if d, err := store.SubagentStopGate(false); err != nil || !d.Allow {
		t.Fatalf("idle gate = %#v err=%v", d, err)
	}
	if _, err := store.LoadPlayBook("flow"); err != nil {
		t.Fatal(err)
	}
	task, err := store.CreateTask("do the thing")
	if err != nil {
		t.Fatal(err)
	}

	// 提交过 → 放行,即便仍有 open 任务。
	if d, err := store.SubagentStopGate(true); err != nil || !d.Allow {
		t.Fatalf("submitted gate = %#v err=%v", d, err)
	}
	// 未提交且当前回合有 open 任务 → 拒绝。
	d, err := store.SubagentStopGate(false)
	if err != nil || d.Allow || d.Reason == "" {
		t.Fatalf("open-unsubmitted gate = %#v err=%v", d, err)
	}
	// 提交后无 open 任务 → 放行。
	if _, err := store.SubmitTask(context.Background(), task.TaskID, "ok", "ok", verify.ResultSpec{Kind: "shell", Command: "exit 0"}); err != nil {
		t.Fatal(err)
	}
	if d, err := store.SubagentStopGate(false); err != nil || !d.Allow {
		t.Fatalf("no-open gate = %#v err=%v", d, err)
	}
}

// 连续拦截到上限 → 放行,对局不被中止(用独立 store,使计数从零起)。
func TestSubagentStopGateCap(t *testing.T) {
	root := repoWithBook(t, "flow.md", twoStepBook)
	store := New(root, "test")
	if _, err := store.LoadPlayBook("flow"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.CreateTask("open work"); err != nil {
		t.Fatal(err)
	}
	allowed, blocks := false, 0
	for i := 0; i < playbook.SubagentBlockCap+2; i++ {
		d, err := store.SubagentStopGate(false)
		if err != nil {
			t.Fatal(err)
		}
		if d.Allow {
			allowed = true
			break
		}
		blocks++
	}
	if !allowed || blocks != playbook.SubagentBlockCap {
		t.Fatalf("cap: allowed=%t blocks=%d, want %d blocks then allow", allowed, blocks, playbook.SubagentBlockCap)
	}
	if show, err := store.ShowStepJob(); err != nil || show.Status != StatusActive {
		t.Fatalf("match status after cap = %#v err=%v, want active", show, err)
	}
}

func TestSubagentSubmitted(t *testing.T) {
	write := func(lines ...string) string {
		t.Helper()
		path := filepath.Join(t.TempDir(), "agent.jsonl")
		body := ""
		for _, l := range lines {
			body += l + "\n"
		}
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
		return path
	}
	toolUse := func(name string) string {
		return `{"type":"assistant","message":{"content":[{"type":"tool_use","name":"` + name + `","id":"x"}]}}`
	}

	// 真有一次 SubmitTask 工具调用 → true。
	if !SubagentSubmitted(write(toolUse("mcp__arbiter-executor__ReviewTask"), toolUse(submitTaskToolName))) {
		t.Fatal("SubmitTask tool_use not detected")
	}
	// 只调用了别的工具 → false(没提交)。
	if SubagentSubmitted(write(toolUse("mcp__arbiter-executor__ReviewTask"), toolUse("Read"))) {
		t.Fatal("non-submit transcript reported as submitted")
	}
	// 文本里提到 SubmitTask 但没有结构化工具调用 → false(不靠字符匹配)。
	if SubagentSubmitted(write(`{"type":"assistant","message":{"content":[{"type":"text","text":"I will call SubmitTask now"}]}}`)) {
		t.Fatal("prose mention of SubmitTask must not count as a call")
	}
	// 文件缺失 → false。
	if SubagentSubmitted(filepath.Join(t.TempDir(), "missing.jsonl")) {
		t.Fatal("missing transcript reported as submitted")
	}
	if SubagentSubmitted("") {
		t.Fatal("empty path reported as submitted")
	}
}

func TestResolveSubagentTranscript(t *testing.T) {
	project := t.TempDir()
	main := filepath.Join(project, "session-1.jsonl")
	derivedDir := filepath.Join(project, "session-1", "subagents")
	if err := os.MkdirAll(derivedDir, 0o755); err != nil {
		t.Fatal(err)
	}
	derived := filepath.Join(derivedDir, "agent-a1b2.jsonl")
	if err := os.WriteFile(derived, []byte("{}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := ResolveSubagentTranscript(main, "a1b2"); got != derived {
		t.Fatalf("derived = %q, want %q", got, derived)
	}
	if got := ResolveSubagentTranscript(main, "missing"); got != main {
		t.Fatalf("fallback = %q", got)
	}
	if got := ResolveSubagentTranscript(main, ""); got != main {
		t.Fatalf("no-agent-id = %q", got)
	}
	if got := ResolveSubagentTranscript("", "a1b2"); got != "" {
		t.Fatalf("empty path = %q", got)
	}
}
