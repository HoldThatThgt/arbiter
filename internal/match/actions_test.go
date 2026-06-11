package match

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
	"github.com/HoldThatThgt/arbiter/internal/verify"
)

const twoStepBook = `---
name: flow
description: test flow
---

[STEP] first
[StepJob]
do first
[CheckList]
- first done
[Branch]
success: second
failure: first

[STEP] second
[StepJob]
do second
[CheckList]
- second done
[Branch]
success: END
failure: first
`

const loopBook = `---
name: loop
description: loop flow
max_steps: 5
---

[STEP] again
[StepJob]
do again
[CheckList]
- again done
[Branch]
success: again
failure: again
`

func TestMatchSuccessAndReview(t *testing.T) {
	root := repoWithBook(t, "flow.md", twoStepBook)
	store := New(root, "test")

	loaded, err := store.LoadPlayBook("flow")
	if err != nil {
		t.Fatal(err)
	}
	if loaded.FirstStep != "first" {
		t.Fatalf("first step = %q", loaded.FirstStep)
	}
	if out, err := store.CheckStepJob(context.Background()); err != nil || out.Complete || out.Reason != "no_tasks" {
		t.Fatalf("check no tasks = %#v err=%v", out, err)
	}
	task, err := store.CreateTask("prove first")
	if err != nil {
		t.Fatal(err)
	}
	if out, err := store.CheckStepJob(context.Background()); err != nil || out.Complete || len(out.OpenTasks) != 1 {
		t.Fatalf("check open = %#v err=%v", out, err)
	}
	submitted, err := store.SubmitTask(context.Background(), task.TaskID, "first proven", "done", verify.ResultSpec{Kind: "shell", Command: "exit 0"})
	if err != nil {
		t.Fatal(err)
	}
	if submitted.Verdict != TaskPass {
		t.Fatalf("verdict = %q", submitted.Verdict)
	}
	out, err := store.CheckStepJob(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !out.Complete || out.NextStep != "second" || out.Round != 2 {
		t.Fatalf("check success = %#v", out)
	}
	review, err := store.ReviewTask(task.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	if !review.Archived || review.Round != 1 || review.Status != TaskPass || review.Summary != "first proven" {
		t.Fatalf("review = %#v", review)
	}
}

func TestFailureBranchAndFinish(t *testing.T) {
	root := repoWithBook(t, "flow.md", twoStepBook)
	store := New(root, "test")
	if _, err := store.LoadPlayBook("flow"); err != nil {
		t.Fatal(err)
	}
	task, err := store.CreateTask("fail first")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.SubmitTask(context.Background(), task.TaskID, "first failed", "failed", verify.ResultSpec{Kind: "shell", Command: "exit 7"}); err != nil {
		t.Fatal(err)
	}
	out, err := store.CheckStepJob(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if out.NextStep != "first" {
		t.Fatalf("failure branch = %#v", out)
	}
}

func TestStepsExhausted(t *testing.T) {
	root := repoWithBook(t, "loop.md", loopBook)
	store := New(root, "test")
	if _, err := store.LoadPlayBook("loop"); err != nil {
		t.Fatal(err)
	}
	var out CheckStepJobOutput
	rounds := 0
	for i := 0; i < 10; i++ {
		task, err := store.CreateTask("loop")
		if err != nil {
			t.Fatal(err)
		}
		if _, err := store.SubmitTask(context.Background(), task.TaskID, "looped", "done", verify.ResultSpec{Kind: "shell", Command: "exit 0"}); err != nil {
			t.Fatal(err)
		}
		out, err = store.CheckStepJob(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		rounds++
		if out.Match == StatusAborted {
			break
		}
	}
	if out.Match != StatusAborted || out.Abort != AbortStepsExhausted {
		t.Fatalf("budget result = %#v", out)
	}
	if rounds != 5 { // max_steps: 5 → 第 5 回合裁决后预算耗尽
		t.Fatalf("rounds = %d", rounds)
	}
}

func TestReplaceLoad(t *testing.T) {
	root := repoWithBook(t, "flow.md", twoStepBook)
	store := New(root, "test")
	first, err := store.LoadPlayBook("flow")
	if err != nil {
		t.Fatal(err)
	}
	second, err := store.LoadPlayBook("flow")
	if err != nil {
		t.Fatal(err)
	}
	if second.ReplacedMatch == nil || *second.ReplacedMatch != first.MatchID {
		t.Fatalf("replace = %#v first=%s", second.ReplacedMatch, first.MatchID)
	}
}

func TestLoadPlayBookPinsRecipes(t *testing.T) {
	root := repoWithBook(t, "flow.md", twoStepBook)
	writeRecipes(t, root, "unit", "cmd: make test\n")
	store := New(root, "test")
	if _, err := store.LoadPlayBook("flow"); err != nil {
		t.Fatal(err)
	}
	state := readStateFile(t, root)
	pin := state.RecipesPin
	if pin.BookSHA256 == "" || pin.Targets["unit"] == "" {
		t.Fatalf("recipes pin = %#v", pin)
	}
}

func TestRunKindRecipePinMismatch(t *testing.T) {
	root := repoWithBook(t, "flow.md", twoStepBook)
	writeRecipes(t, root, "unit", "cmd: make test\n")
	store := New(root, "test")
	if _, err := store.LoadPlayBook("flow"); err != nil {
		t.Fatal(err)
	}
	task, err := store.CreateTask("run unit")
	if err != nil {
		t.Fatal(err)
	}
	writeRecipes(t, root, "unit", "cmd: make test CHANGED=1\n")
	_, err = store.SubmitTask(context.Background(), task.TaskID, "unit", "r", verify.ResultSpec{
		Kind:   "run",
		Recipe: "unit",
		Tests:  []string{"Suite.Case"},
		Expect: json.RawMessage(`{"overall":"passed"}`),
	})
	if code := toolCode(err); code != playbook.CodeRecipePinMismatch {
		t.Fatalf("code = %q, want %q (err=%v)", code, playbook.CodeRecipePinMismatch, err)
	}
	data, err := os.ReadFile(filepath.Join(root, ".arbiter", "match", "log", "journal.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(data), "recipe_pin_mismatch") {
		t.Fatalf("journal = %s", data)
	}
}

func TestSubmitTaskSummaryValidation(t *testing.T) {
	root := repoWithBook(t, "flow.md", twoStepBook)
	store := New(root, "test")
	if _, err := store.LoadPlayBook("flow"); err != nil {
		t.Fatal(err)
	}
	task, err := store.CreateTask("prove it")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.SubmitTask(context.Background(), task.TaskID, "  ", "r", verify.ResultSpec{Kind: "shell", Command: "exit 0"}); toolCode(err) != playbook.CodeBadSummary {
		t.Fatalf("empty summary err = %#v", err)
	}
	long := strings.Repeat("x", playbook.MaxSummaryBytes+1)
	if _, err := store.SubmitTask(context.Background(), task.TaskID, long, "r", verify.ResultSpec{Kind: "shell", Command: "exit 0"}); toolCode(err) != playbook.CodeBadSummary {
		t.Fatalf("oversize summary err = %#v", err)
	}
	if _, err := store.SubmitTask(context.Background(), task.TaskID, " proven ok ", "r", verify.ResultSpec{Kind: "shell", Command: "exit 0"}); err != nil {
		t.Fatal(err)
	}
	show, err := store.ShowStepJob()
	if err != nil {
		t.Fatal(err)
	}
	if len(show.Tasks) != 1 || show.Tasks[0].Summary != "proven ok" {
		t.Fatalf("show tasks = %#v", show.Tasks)
	}
	status, err := os.ReadFile(filepath.Join(root, ".arbiter", "match", "status.json"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(status), "proven ok") {
		t.Fatalf("status = %s", status)
	}
}

func writeRecipes(t *testing.T, root, id, body string) {
	t.Helper()
	path := filepath.Join(root, ".arbiter", "recipes.yaml")
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	text := "targets:\n  " + id + ":\n"
	for _, line := range strings.Split(strings.TrimSuffix(body, "\n"), "\n") {
		text += "    " + line + "\n"
	}
	if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
		t.Fatal(err)
	}
}

func readStateFile(t *testing.T, root string) Match {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(root, ".arbiter", "match", "run", "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	var state Match
	if err := json.Unmarshal(data, &state); err != nil {
		t.Fatal(err)
	}
	return state
}

func TestListTask(t *testing.T) {
	root := repoWithBook(t, "flow.md", twoStepBook)
	store := New(root, "test")
	if _, err := store.ListTask(); toolCode(err) != playbook.CodeNoMatchLoaded {
		t.Fatalf("idle err = %#v", err)
	}
	if _, err := store.LoadPlayBook("flow"); err != nil {
		t.Fatal(err)
	}
	t1, err := store.CreateTask("a")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.SubmitTask(context.Background(), t1.TaskID, "a done", "r", verify.ResultSpec{Kind: "shell", Command: "exit 0"}); err != nil {
		t.Fatal(err)
	}
	if _, err := store.CheckStepJob(context.Background()); err != nil {
		t.Fatal(err)
	}
	t2, err := store.CreateTask("b")
	if err != nil {
		t.Fatal(err)
	}
	out, err := store.ListTask()
	if err != nil {
		t.Fatal(err)
	}
	if len(out.Tasks) != 2 {
		t.Fatalf("tasks = %#v", out.Tasks)
	}
	first, second := out.Tasks[0], out.Tasks[1]
	if first.TaskID != t1.TaskID || first.Round != 1 || first.StepID != "first" || first.Status != TaskPass || first.Summary != "a done" {
		t.Fatalf("first = %#v", first)
	}
	if second.TaskID != t2.TaskID || second.Round != 2 || second.StepID != "second" || second.Status != TaskOpen || second.Summary != "" {
		t.Fatalf("second = %#v", second)
	}
}

func TestNotePlaybook(t *testing.T) {
	root := repoWithBook(t, "flow.md", twoStepBook)
	store := New(root, "test")
	if _, err := store.NotePlaybook("first", "n"); toolCode(err) != playbook.CodeNoMatchLoaded {
		t.Fatalf("idle err = %#v", err)
	}
	if _, err := store.LoadPlayBook("flow"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.NotePlaybook("first", " "); toolCode(err) != playbook.CodeBadNote {
		t.Fatalf("empty note err = %#v", err)
	}
	if _, err := store.NotePlaybook("first", "line1\nline2"); toolCode(err) != playbook.CodeBadNote {
		t.Fatalf("multiline err = %#v", err)
	}
	if _, err := store.NotePlaybook("first", strings.Repeat("x", playbook.MaxNoteBytes+1)); toolCode(err) != playbook.CodeBadNote {
		t.Fatalf("oversize err = %#v", err)
	}
	if _, err := store.NotePlaybook("second", "future step"); toolCode(err) != playbook.CodeStepNotFound {
		t.Fatalf("unvisited err = %#v", err)
	}

	noted, err := store.NotePlaybook("first", "watch the cache")
	if err != nil {
		t.Fatal(err)
	}
	if !noted.Added || noted.Playbook != "flow" || len(noted.Gotchas) != 1 {
		t.Fatalf("noted = %#v", noted)
	}
	data, err := os.ReadFile(filepath.Join(root, ".arbiter", "match", "playbook", "flow.md"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(data), "[Gotcha]\n- watch the cache") {
		t.Fatalf("file = %s", data)
	}
	show, err := store.ShowStepJob()
	if err != nil {
		t.Fatal(err)
	}
	if show.Step == nil || len(show.Step.Gotchas) != 1 || show.Step.Gotchas[0] != "watch the cache" {
		t.Fatalf("show = %#v", show.Step)
	}

	dup, err := store.NotePlaybook("first", "watch the cache")
	if err != nil {
		t.Fatal(err)
	}
	if dup.Added || len(dup.Gotchas) != 1 {
		t.Fatalf("dup = %#v", dup)
	}

	// 推进到 second:first 进入历史,仍可补记
	task, err := store.CreateTask("go")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.SubmitTask(context.Background(), task.TaskID, "ok", "r", verify.ResultSpec{Kind: "shell", Command: "exit 0"}); err != nil {
		t.Fatal(err)
	}
	if _, err := store.CheckStepJob(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := store.NotePlaybook("first", "second thought"); err != nil {
		t.Fatal(err)
	}

	// 重新装载:注记已沉淀进棋谱文件,新对局快照直接可见
	if _, err := store.LoadPlayBook("flow"); err != nil {
		t.Fatal(err)
	}
	show, err = store.ShowStepJob()
	if err != nil {
		t.Fatal(err)
	}
	if len(show.Step.Gotchas) != 2 {
		t.Fatalf("reloaded gotchas = %#v", show.Step.Gotchas)
	}
}

func TestNotePlaybookAfterFinish(t *testing.T) {
	const oneStep = `---
name: once
description: one step
---

[STEP] only
[StepJob]
do it
[CheckList]
- done
[Branch]
success: END
failure: END
`
	root := repoWithBook(t, "once.md", oneStep)
	store := New(root, "test")
	if _, err := store.LoadPlayBook("once"); err != nil {
		t.Fatal(err)
	}
	task, err := store.CreateTask("t")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.SubmitTask(context.Background(), task.TaskID, "ok", "r", verify.ResultSpec{Kind: "shell", Command: "exit 0"}); err != nil {
		t.Fatal(err)
	}
	out, err := store.CheckStepJob(context.Background())
	if err != nil || out.Match != StatusFinishedSuccess {
		t.Fatalf("check = %#v err=%v", out, err)
	}
	// 终局后的复盘窗口仍可补记(步骤经历史回合可见)
	noted, err := store.NotePlaybook("only", "post-mortem note")
	if err != nil {
		t.Fatal(err)
	}
	if !noted.Added {
		t.Fatalf("noted = %#v", noted)
	}
	data, err := os.ReadFile(filepath.Join(root, ".arbiter", "match", "playbook", "once.md"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(data), "- post-mortem note") {
		t.Fatalf("file = %s", data)
	}
}

func repoWithBook(t *testing.T, name, body string) string {
	t.Helper()
	root := t.TempDir()
	dir := filepath.Join(root, ".arbiter", "match", "playbook")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return root
}
