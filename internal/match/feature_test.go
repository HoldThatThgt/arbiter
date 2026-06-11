package match

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"syscall"
	"testing"
	"time"

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

func TestGoalMemoDefaultOff(t *testing.T) {
	root := repoWithBook(t, "g.md", goalBook("exit 0"))
	store := New(root, "test")
	if store.goalMemoEnabled() {
		t.Fatal("goal memo should default off")
	}
	writeConfig(t, root, "match:\n  goal_memo: false\n")
	if store.goalMemoEnabled() {
		t.Fatal("goal memo false should stay off")
	}
	writeConfig(t, root, "match:\n  goal_memo: true\n")
	if !store.goalMemoEnabled() {
		t.Fatal("goal memo true should enable memoization")
	}
}

func TestGoalMemoizedPassSkipsShellGoalWhenEnabled(t *testing.T) {
	root := repoWithBook(t, "g.md", goalBook("sh -c 'echo ran >> goal.log; exit 0'"))
	writeConfig(t, root, "match:\n  goal_memo: true\n")
	writeText(t, filepath.Join(root, "src", "a.c"), "int a;\n")
	store := New(root, "test")
	if _, err := store.LoadPlayBook("goalflow"); err != nil {
		t.Fatal(err)
	}
	seedGoalMemo(t, store)

	out := passRound(t, store)

	if !out.Checkmate || out.Goal == nil || !out.Goal.Memoized {
		t.Fatalf("memoized goal did not checkmate: %#v", out)
	}
	if _, err := os.Stat(filepath.Join(root, "goal.log")); !os.IsNotExist(err) {
		t.Fatalf("goal command ran despite memo: %v", err)
	}
}

func TestGoalMemoInvalidatesOnNewFile(t *testing.T) {
	root := repoWithBook(t, "g.md", goalBook("sh -c 'echo ran >> goal.log; exit 0'"))
	writeConfig(t, root, "match:\n  goal_memo: true\n")
	writeText(t, filepath.Join(root, "src", "a.c"), "int a;\n")
	store := New(root, "test")
	if _, err := store.LoadPlayBook("goalflow"); err != nil {
		t.Fatal(err)
	}
	seedGoalMemo(t, store)
	writeText(t, filepath.Join(root, "src", "new.c"), "int fresh;\n")

	out := passRound(t, store)

	if !out.Checkmate || out.Goal == nil || out.Goal.Memoized {
		t.Fatalf("new file should force goal execution: %#v", out)
	}
	if data := readText(t, filepath.Join(root, "goal.log")); data != "ran\n" {
		t.Fatalf("goal log = %q", data)
	}
}

func TestGoalMemoRecordedWhenWorkspaceUnchanged(t *testing.T) {
	root := repoWithBook(t, "g.md", goalBook("exit 0"))
	writeConfig(t, root, "match:\n  goal_memo: true\n")
	writeText(t, filepath.Join(root, "src", "a.c"), "int a;\n")
	store := New(root, "test")
	if _, err := store.LoadPlayBook("goalflow"); err != nil {
		t.Fatal(err)
	}

	out := passRound(t, store)

	if !out.Checkmate || out.Goal == nil || out.Goal.Memoized {
		t.Fatalf("checkmate = %#v", out)
	}
	state := readStateFile(t, root)
	if len(state.GoalMemo) != 1 {
		t.Fatalf("goal memo = %#v", state.GoalMemo)
	}
}

func TestGoalMemoSkippedWhenGoalMutatesWorkspace(t *testing.T) {
	// goal 谓词本身改写工作区:执行前后普查不一致,绝不能把这次 pass 记入 memo(TOCTOU)。
	root := repoWithBook(t, "g.md", goalBook("sh -c 'echo mutated > side-effect.txt; exit 0'"))
	writeConfig(t, root, "match:\n  goal_memo: true\n")
	writeText(t, filepath.Join(root, "src", "a.c"), "int a;\n")
	store := New(root, "test")
	if _, err := store.LoadPlayBook("goalflow"); err != nil {
		t.Fatal(err)
	}

	out := passRound(t, store)

	if !out.Checkmate || out.Goal == nil || out.Goal.Verdict != TaskPass {
		t.Fatalf("checkmate = %#v", out)
	}
	if data := readText(t, filepath.Join(root, "side-effect.txt")); data != "mutated\n" {
		t.Fatalf("side effect = %q", data)
	}
	state := readStateFile(t, root)
	if len(state.GoalMemo) != 0 {
		t.Fatalf("memo recorded despite workspace mutation: %#v", state.GoalMemo)
	}
}

func TestGoalCensusDigestHazards(t *testing.T) {
	root := t.TempDir()
	store := New(root, "test")
	writeText(t, filepath.Join(root, "src", "a.c"), "int a;\n")
	base, ok := store.goalCensusDigest()
	if !ok || base == "" {
		t.Fatalf("base census ok=%v digest=%q", ok, base)
	}

	// FIFO 不参与普查也不阻塞/报错
	if err := syscall.Mkfifo(filepath.Join(root, "pipe"), 0o644); err != nil {
		t.Fatal(err)
	}
	withFifo, ok := store.goalCensusDigest()
	if !ok || withFifo != base {
		t.Fatalf("fifo must be skipped: ok=%v changed=%v", ok, withFifo != base)
	}

	// 坏符号链接按链接目标参与普查,不报错
	if err := os.Symlink("missing-target", filepath.Join(root, "dangling")); err != nil {
		t.Fatal(err)
	}
	withLink, ok := store.goalCensusDigest()
	if !ok {
		t.Fatal("dangling symlink must not disable census")
	}
	if withLink == base {
		t.Fatal("symlink should participate in census digest")
	}

	// 不可读的常规文件:禁用本次 memo(ok=false),而不是报错
	if os.Geteuid() == 0 {
		t.Log("running as root: skipping unreadable-file case")
		return
	}
	secret := filepath.Join(root, "secret.txt")
	writeText(t, secret, "hidden\n")
	if err := os.Chmod(secret, 0o000); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(secret, 0o644) })
	if _, ok := store.goalCensusDigest(); ok {
		t.Fatal("unreadable regular file must disable memoization")
	}
}

func TestGoalMemoCapEviction(t *testing.T) {
	m := &Match{}
	for i := 0; i < goalMemoCap+5; i++ {
		rememberGoalMemo(m, fmt.Sprintf("digest-%03d", i), &GoalReport{Verdict: TaskPass})
	}
	if len(m.GoalMemo) != goalMemoCap {
		t.Fatalf("memo size = %d, want %d", len(m.GoalMemo), goalMemoCap)
	}
	if _, ok := m.GoalMemo[fmt.Sprintf("digest-%03d", goalMemoCap+4)]; !ok {
		t.Fatalf("newest entry evicted: %#v", m.GoalMemo)
	}
}

func TestAsyncRunGoalFalseCheckmateAcrossRestart(t *testing.T) {
	const runGoalBook = `---
name: run-goal
description: async run goal
---

[SetGoal]
run: unit
tests: ["Suite.Case"]
expect: {"overall":"passed"}

[STEP] only
[StepJob]
finish
[CheckList]
- done
[Branch]
success: END
failure: only
`
	root := repoWithBook(t, "run.md", runGoalBook)
	t.Setenv("PYTHONPATH", checkoutEnginePath(t))
	// 引擎对 kind=="run" 永远执行真实 recipe(stub 分支不可达):用一个必败的假 gtest。
	script := writeFakeGtest(t, root, true)
	writeRecipes(t, root, "unit", "harness:\n  kind: gtest\ntest_run:\n  cmd: ["+script+"]\n")
	store := New(root, "test")
	if _, err := store.LoadPlayBook("run-goal"); err != nil {
		t.Fatal(err)
	}
	task, err := store.CreateTask("pass step")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.SubmitTask(context.Background(), task.TaskID, "step done", "done", verify.ResultSpec{Kind: "shell", Command: "exit 0"}); err != nil {
		t.Fatal(err)
	}

	started, err := store.CheckStepJob(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if started.Complete || started.Reason != "goal_running" || started.RunID == "" {
		t.Fatalf("started = %#v", started)
	}

	restarted := New(root, "test")
	var settled CheckStepJobOutput
	for deadline := time.Now().Add(10 * time.Second); time.Now().Before(deadline); {
		settled, err = restarted.CheckStepJob(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		if settled.Reason != "goal_running" {
			break
		}
		time.Sleep(25 * time.Millisecond)
	}
	if settled.Checkmate || settled.Match != StatusFinishedFailure {
		t.Fatalf("settled = %#v", settled)
	}
	if settled.Goal == nil || settled.Goal.Verdict != TaskFail || settled.Goal.IsError == nil || *settled.Goal.IsError {
		t.Fatalf("goal = %#v", settled.Goal)
	}
}

func checkoutEnginePath(t *testing.T) string {
	t.Helper()
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("runtime.Caller failed")
	}
	return filepath.Clean(filepath.Join(filepath.Dir(file), "..", "..", "engine"))
}

// writeFakeGtest 写一个伪 gtest 可执行:解析 --gtest_output=xml:<path>,
// 落一份单用例的 gtest XML(failed 控制成败)后按结果退出。
func writeFakeGtest(t *testing.T, root string, failed bool) string {
	t.Helper()
	xml := `<testsuites tests="1" failures="0"><testsuite name="Suite"><testcase classname="Suite" name="Case" time="0.001"/></testsuite></testsuites>`
	exit := "exit 0"
	if failed {
		xml = `<testsuites tests="1" failures="1"><testsuite name="Suite"><testcase classname="Suite" name="Case"><failure message="bad"/></testcase></testsuite></testsuites>`
		exit = "exit 1"
	}
	body := "#!/bin/sh\n" +
		"for arg in \"$@\"; do\n" +
		"  case \"$arg\" in --gtest_output=xml:*) out=\"${arg#--gtest_output=xml:}\" ;; esac\n" +
		"done\n" +
		"mkdir -p \"$(dirname \"$out\")\"\n" +
		"printf '%s\\n' '" + xml + "' > \"$out\"\n" +
		exit + "\n"
	path := filepath.Join(root, "fake_gtest.sh")
	if err := os.WriteFile(path, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	return path
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
	if _, err := os.Stat(filepath.Join(root, ".arbiter", "playbook", "goalflow.md")); err != nil {
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

func writeConfig(t *testing.T, root, text string) {
	t.Helper()
	writeText(t, filepath.Join(root, ".arbiter", "config.yml"), text)
}

func writeText(t *testing.T, path, text string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
		t.Fatal(err)
	}
}

func seedGoalMemo(t *testing.T, store *Store) {
	t.Helper()
	if _, err := store.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Playbook.Goal == nil {
			t.Fatal("missing match goal")
		}
		digest, err := store.goalMemoDigest(m, *m.Playbook.Goal)
		if err != nil {
			return nil, nil, err
		}
		m.GoalMemo = map[string]GoalMemoEntry{
			digest: {
				Report: GoalReport{
					Verdict: TaskPass,
					Output:  "memoized pass",
				},
			},
		}
		return m, nil, nil
	}); err != nil {
		t.Fatal(err)
	}
}

func readText(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}
