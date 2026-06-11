package match

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
	"github.com/HoldThatThgt/arbiter/internal/verify"
)

func goalBook(goalCmd string) string {
	return `---
name: goalflow
description: goal flow
max_steps: 6
---

[SetGoal]
shell: ` + goalCmd + `

[STEP] work
[StepJob]
do work
[CheckList]
- done
[Branch]
success: polish
failure: work

[STEP] polish
[StepJob]
polish it
[CheckList]
- done
[Branch]
success: END
failure: work
`
}

func passRound(t *testing.T, store *Store) CheckStepJobOutput {
	t.Helper()
	task, err := store.CreateTask("work")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.SubmitTask(context.Background(), task.TaskID, "work done", "done", verify.ResultSpec{Kind: "shell", Command: "exit 0"}); err != nil {
		t.Fatal(err)
	}
	out, err := store.CheckStepJob(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func TestCheckmateOnAnySuccessRound(t *testing.T) {
	root := repoWithBook(t, "g.md", goalBook("exit 0"))
	store := New(root, "test")
	if _, err := store.LoadPlayBook("goalflow"); err != nil {
		t.Fatal(err)
	}
	out := passRound(t, store)
	if !out.Checkmate || out.Match != StatusFinishedSuccess {
		t.Fatalf("checkmate = %#v", out)
	}
	if out.Goal == nil || out.Goal.Verdict != TaskPass {
		t.Fatalf("goal report = %#v", out.Goal)
	}
}

func TestGoalFailContinuesThenUnmetAtEnd(t *testing.T) {
	root := repoWithBook(t, "g.md", goalBook("exit 1"))
	store := New(root, "test")
	if _, err := store.LoadPlayBook("goalflow"); err != nil {
		t.Fatal(err)
	}
	out := passRound(t, store) // 回合1成功,goal 未过 → 继续 success 分支
	if out.Checkmate || out.NextStep != "polish" || out.Goal == nil || out.Goal.Verdict != TaskFail {
		t.Fatalf("continue = %#v", out)
	}
	out = passRound(t, store) // 回合2走到 END,goal 仍未过 → 失败终局
	if out.Match != StatusFinishedFailure || out.Checkmate {
		t.Fatalf("end unmet = %#v", out)
	}
}

func TestAddPlayBook(t *testing.T) {
	root := repoWithBook(t, "flow.md", twoStepBook)
	store := New(root, "test")

	added, err := store.AddPlayBook(goalBook("exit 0"))
	if err != nil {
		t.Fatal(err)
	}
	if added.Name != "goalflow" || added.StepsTotal != 2 || !added.HasGoal || added.MaxSteps != 6 {
		t.Fatalf("added = %#v", added)
	}
	if _, err := os.Stat(filepath.Join(root, ".arbiter", "match", "playbook", "goalflow.md")); err != nil {
		t.Fatal(err)
	}
	if _, err := store.LoadPlayBook("goalflow"); err != nil {
		t.Fatal(err)
	}

	if _, err := store.AddPlayBook(goalBook("exit 0")); toolCode(err) != playbook.CodeNameConflict {
		t.Fatalf("dup err = %#v", err)
	}
	if _, err := store.AddPlayBook("not a playbook"); toolCode(err) != playbook.CodePlaybookInvalid {
		t.Fatalf("invalid err = %#v", err)
	}
	evil := "---\nname: ../escape\ndescription: d\n---\n\n[STEP] s\n[StepJob]\nx\n[CheckList]\n- a\n[Branch]\nsuccess: END\nfailure: s\n"
	if _, err := store.AddPlayBook(evil); toolCode(err) != playbook.CodePlaybookInvalid {
		t.Fatalf("traversal err = %#v", err)
	}
}

func TestStopGate(t *testing.T) {
	root := repoWithBook(t, "flow.md", twoStepBook)
	store := New(root, "test")

	if d, err := store.StopGate(); err != nil || !d.Allow {
		t.Fatalf("idle gate = %#v err=%v", d, err)
	}
	if _, err := store.LoadPlayBook("flow"); err != nil {
		t.Fatal(err)
	}
	d, err := store.StopGate()
	if err != nil || d.Allow || d.Reason == "" {
		t.Fatalf("active gate = %#v err=%v", d, err)
	}

	// 推进一个回合后计数应清零
	task, _ := store.CreateTask("t")
	if _, err := store.SubmitTask(context.Background(), task.TaskID, "ok", "ok", verify.ResultSpec{Kind: "shell", Command: "exit 0"}); err != nil {
		t.Fatal(err)
	}
	if _, err := store.CheckStepJob(context.Background()); err != nil {
		t.Fatal(err)
	}
	if blocks := readStopBlocks(t, root); blocks != 0 {
		t.Fatalf("blocks after round = %d", blocks)
	}

	// 连续拦截到上限 → 中止对局并放行
	allowed := false
	for i := 0; i < playbook.StopBlockCap+2; i++ {
		d, err := store.StopGate()
		if err != nil {
			t.Fatal(err)
		}
		if d.Allow {
			allowed = true
			break
		}
	}
	if !allowed {
		t.Fatal("gate never allowed")
	}
	show, err := store.ShowStepJob()
	if err != nil {
		t.Fatal(err)
	}
	if show.Status != StatusAborted || show.Abort != AbortStopLimit {
		t.Fatalf("after cap = %#v", show)
	}
	if d, err := store.StopGate(); err != nil || !d.Allow {
		t.Fatalf("terminal gate = %#v err=%v", d, err)
	}
}

func toolCode(err error) string {
	if terr, ok := err.(*ToolError); ok {
		return terr.Code
	}
	return ""
}

func readStopBlocks(t *testing.T, root string) int {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(root, ".arbiter", "match", "run", "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	var m Match
	if err := json.Unmarshal(data, &m); err != nil {
		t.Fatal(err)
	}
	return m.StopBlocks
}
