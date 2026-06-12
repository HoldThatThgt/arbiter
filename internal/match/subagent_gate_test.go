package match

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
	"github.com/HoldThatThgt/arbiter/internal/verify"
)

func TestSubagentStopGate(t *testing.T) {
	root := repoWithBook(t, "flow.md", twoStepBook)
	store := New(root, "test")

	// 无对局:放行
	if d, err := store.SubagentStopGate([]string{"T1"}); err != nil || !d.Allow {
		t.Fatalf("idle gate = %#v err=%v", d, err)
	}
	if _, err := store.LoadPlayBook("flow"); err != nil {
		t.Fatal(err)
	}

	task, err := store.CreateTask("do the thing")
	if err != nil {
		t.Fatal(err)
	}
	// open task → 拒绝,reason 点名 task id
	d, err := store.SubagentStopGate([]string{task.TaskID})
	if err != nil || d.Allow || !strings.Contains(d.Reason, task.TaskID) {
		t.Fatalf("open gate = %#v err=%v", d, err)
	}
	// 不在局的 id / 空候选:放行
	if d, err := store.SubagentStopGate([]string{"T999"}); err != nil || !d.Allow {
		t.Fatalf("unknown-id gate = %#v err=%v", d, err)
	}
	if d, err := store.SubagentStopGate(nil); err != nil || !d.Allow {
		t.Fatalf("no-id gate = %#v err=%v", d, err)
	}
	// 已交 → 放行
	if _, err := store.SubmitTask(context.Background(), task.TaskID, "ok", "ok", verify.ResultSpec{Kind: "shell", Command: "exit 0"}); err != nil {
		t.Fatal(err)
	}
	if d, err := store.SubagentStopGate([]string{task.TaskID}); err != nil || !d.Allow {
		t.Fatalf("submitted gate = %#v err=%v", d, err)
	}

	// 连续拦截到上限 → 放行,但对局不被中止(区别于 StopGate 的 abort)
	task2, err := store.CreateTask("again")
	if err != nil {
		t.Fatal(err)
	}
	allowed := false
	blocks := 0
	for i := 0; i < playbook.SubagentBlockCap+2; i++ {
		d, err := store.SubagentStopGate([]string{task2.TaskID})
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
		t.Fatalf("cap: allowed=%t blocks=%d", allowed, blocks)
	}
	show, err := store.ShowStepJob()
	if err != nil {
		t.Fatal(err)
	}
	if show.Status != StatusActive {
		t.Fatalf("match status after cap = %q, want active", show.Status)
	}
}

func TestExtractDispatchTaskIDs(t *testing.T) {
	write := func(lines ...string) string {
		t.Helper()
		path := filepath.Join(t.TempDir(), "agent.jsonl")
		if err := os.WriteFile(path, []byte(strings.Join(lines, "\n")+"\n"), 0o644); err != nil {
			t.Fatal(err)
		}
		return path
	}
	userStr := func(text string) string {
		return `{"type":"user","message":{"role":"user","content":` + jsonString(t, text) + `}}`
	}

	// 规程标注行(含非 T<n> 形态的 id)
	got := ExtractDispatchTaskIDs(write(userStr("task id: fix-42\ntask: do X\nfinish: SubmitTask")))
	if len(got) != 1 || got[0] != "fix-42" {
		t.Fatalf("labeled = %#v", got)
	}
	// 自由转述 + 去重保序("dispatched for task T2"——GLM 实测形态)
	got = ExtractDispatchTaskIDs(write(userStr("You are an arbiter-executor dispatched for task T2. Cross-check T2 then T10.")))
	if len(got) != 2 || got[0] != "T2" || got[1] != "T10" {
		t.Fatalf("freestyle = %#v", got)
	}
	// content-block 数组形态
	got = ExtractDispatchTaskIDs(write(`{"type":"user","message":{"role":"user","content":[{"type":"text","text":"task id: T3"}]}}`))
	if len(got) != 1 || got[0] != "T3" {
		t.Fatalf("blocks = %#v", got)
	}
	// 只看首条用户消息:它没有 id 就不看后面的回显
	got = ExtractDispatchTaskIDs(write(
		`{"type":"queue","queue":1}`,
		userStr("no ids in the dispatch prompt"),
		userStr("task id: T9"),
	))
	if got != nil {
		t.Fatalf("first-message-only = %#v", got)
	}
	// 文件不存在 → fail-open
	if got := ExtractDispatchTaskIDs(filepath.Join(t.TempDir(), "missing.jsonl")); got != nil {
		t.Fatalf("missing file = %#v", got)
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
	// 推导文件不存在 / agent id 缺失 → 原路径
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

func jsonString(t *testing.T, s string) string {
	t.Helper()
	b := strings.Builder{}
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		default:
			b.WriteRune(r)
		}
	}
	b.WriteByte('"')
	return b.String()
}
