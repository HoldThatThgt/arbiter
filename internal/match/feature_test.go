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

func TestGoalMemoDisabledForRecipesCapability(t *testing.T) {
	// recipes 能力对局允许 recipes.yaml 中途漂移,而 memo 摘要折入的是冻结 pin、
	// 普查又跳过 .arbiter/,故被削弱的配方仍会命中旧 PASS。最保守的修法:这类对局
	// 一律不 memo —— goalMemoDigest 返回空摘要,调用方既不查也不记。
	root := t.TempDir()
	store := New(root, "test")
	writeText(t, filepath.Join(root, "src", "a.c"), "int a;\n")
	spec := playbook.ResultSpec{Kind: "shell", Command: "exit 0"}

	recipesMatch := &Match{Playbook: playbook.Playbook{Capabilities: []string{"recipes"}}}
	digest, err := store.goalMemoDigest(recipesMatch, spec)
	if err != nil {
		t.Fatalf("recipes match digest err = %v", err)
	}
	if digest != "" {
		t.Fatalf("recipes-capability match must skip memo: digest = %q, want empty", digest)
	}

	// 同一工作区/谓词,非 recipes 能力仍照常产出非空摘要(继续 memo)。
	plainMatch := &Match{Playbook: playbook.Playbook{Capabilities: []string{"shell"}}}
	digest, err = store.goalMemoDigest(plainMatch, spec)
	if err != nil {
		t.Fatalf("non-recipes match digest err = %v", err)
	}
	if digest == "" {
		t.Fatal("non-recipes match should still memoize: got empty digest")
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

// The async run-goal path (the normal gtest path) must also gate on frozen-test
// integrity: a test tampered before the goal launches must fail the goal at the
// start-time check, never spin up the engine, and never declare checkmate — even
// though the goal predicate (a passing gtest) would otherwise win.
func TestAsyncRunGoalRejectsTamperedFrozenTestAtStart(t *testing.T) {
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
	script := writeFakeGtest(t, root, false) // PASSING gtest — would checkmate if it ran
	writeRecipes(t, root, "unit", "harness:\n  kind: gtest\ntest_run:\n  cmd: ["+script+"]\n")
	testPath := filepath.Join(root, "tests", "repro_test.cc")
	if err := os.MkdirAll(filepath.Dir(testPath), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(testPath, []byte("ORIGINAL\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	store := New(root, "test")
	if _, err := store.LoadPlayBook("run-goal"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.RegisterTest([]string{"tests/repro_test.cc"}); err != nil {
		t.Fatal(err)
	}
	task, err := store.CreateTask("pass step")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.SubmitTask(context.Background(), task.TaskID, "step done", "done", verify.ResultSpec{Kind: "shell", Command: "exit 0"}); err != nil {
		t.Fatal(err)
	}

	// Tamper the frozen test, THEN drive the goal: the start-time gate must refuse
	// to launch the engine and fail the goal.
	if err := os.WriteFile(testPath, []byte("TAMPERED\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	out, err := store.CheckStepJob(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if out.Reason == "goal_running" {
		t.Fatalf("engine launched despite tampered frozen test: %#v", out)
	}
	if out.Checkmate || out.Match == StatusFinishedSuccess {
		t.Fatalf("tampered frozen test won via async goal start: %#v", out)
	}
	if out.Goal == nil || out.Goal.Failure != playbook.CodeFrozenTestModified {
		t.Fatalf("goal = %#v, want failure %s", out.Goal, playbook.CodeFrozenTestModified)
	}
}

// The async run-goal path must reject a run whose worker COMPILED a weakened
// frozen test, even when the test is restored byte-for-byte before the verdict
// is polled. This is the residual TOCTOU the Go-side disk re-hash structurally
// cannot see: pass the round with the test pristine (the start-time gate is
// satisfied), weaken the frozen test while the engine worker is compiling, let
// the worker run the weakened suite to a pass, then restore the original bytes
// before the settling poll. Both the start-time gate and the settle-time disk
// re-hash see pristine content. The worker, however, reports the digest it
// actually hashed at compile time; the settle-time comparison against the frozen
// registry catches the mismatch and refuses the checkmate.
func TestAsyncRunGoalRejectsWeakenRunRestoreRace(t *testing.T) {
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
	testPath := filepath.Join(root, "tests", "repro_test.cc")
	if err := os.MkdirAll(filepath.Dir(testPath), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(testPath, []byte("ORIGINAL\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	// The fake gtest PASSES (would checkmate) and, crucially, restores the frozen
	// test to its original bytes during test_run — which runs AFTER the worker has
	// already hashed the (by then weakened) source. So at settle the disk is
	// pristine and the disk re-hash is satisfied; only the engine-reported
	// compile-time digest can still reveal the tamper.
	script := writeFakeGtestRestoring(t, root, testPath, "ORIGINAL")
	writeRecipes(t, root, "unit", "harness:\n  kind: gtest\ntest_run:\n  cmd: ["+script+"]\n")
	store := New(root, "test")
	if _, err := store.LoadPlayBook("run-goal"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.RegisterTest([]string{"tests/repro_test.cc"}); err != nil {
		t.Fatal(err)
	}
	task, err := store.CreateTask("pass step")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.SubmitTask(context.Background(), task.TaskID, "step done", "done", verify.ResultSpec{Kind: "shell", Command: "exit 0"}); err != nil {
		t.Fatal(err)
	}

	// Drive the goal: the start-time gate sees the pristine test and launches the
	// worker. The worker is a freshly exec'd interpreter, so it will not reach its
	// compile-time hash for many milliseconds — ample room to weaken the test next.
	started, err := store.CheckStepJob(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if started.Reason != "goal_running" || started.RunID == "" {
		t.Fatalf("expected goal_running launch, got %#v", started)
	}

	// Weaken the frozen test now, before the worker hashes it: the worker observes
	// (and "compiles") these bytes, then the fake gtest puts the originals back.
	if err := os.WriteFile(testPath, []byte("TAMPERED\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	var settled CheckStepJobOutput
	for deadline := time.Now().Add(10 * time.Second); time.Now().Before(deadline); {
		settled, err = store.CheckStepJob(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		if settled.Reason != "goal_running" {
			break
		}
		time.Sleep(25 * time.Millisecond)
	}

	if settled.Checkmate || settled.Match == StatusFinishedSuccess {
		t.Fatalf("weakened-then-restored frozen test won via async goal: %#v", settled)
	}
	if settled.Goal == nil || settled.Goal.Failure != playbook.CodeFrozenTestModified {
		t.Fatalf("goal = %#v, want failure %s", settled.Goal, playbook.CodeFrozenTestModified)
	}
	// The fake gtest restored the disk, so the settle-time disk re-hash PASSED;
	// the verdict was forced by the engine-reported compile-time digest alone —
	// exactly the gap this change closes.
	if got := readText(t, testPath); got != "ORIGINAL\n" {
		t.Fatalf("frozen test not restored on disk (disk re-hash would have caught it): %q", got)
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

// writeFakeGtestRestoring 写一个伪 gtest:落一份 PASS 的 gtest XML(若直接采信
// 会将死),随后把 restorePath 复原为 restoreLine+"\n"。复原发生在 test_run 阶段,
// 即 worker 已对(被弱化的)源取过摘要之后 —— 用来复现"通关→弱化→编译→复原→poll"
// 竞态:落子那一刻盘面已复原,磁盘复算 frozenViolation 看不出端倪。
func writeFakeGtestRestoring(t *testing.T, root, restorePath, restoreLine string) string {
	t.Helper()
	xml := `<testsuites tests="1" failures="0"><testsuite name="Suite"><testcase classname="Suite" name="Case" time="0.001"/></testsuite></testsuites>`
	body := "#!/bin/sh\n" +
		"for arg in \"$@\"; do\n" +
		"  case \"$arg\" in --gtest_output=xml:*) out=\"${arg#--gtest_output=xml:}\" ;; esac\n" +
		"done\n" +
		"mkdir -p \"$(dirname \"$out\")\"\n" +
		"printf '%s\\n' '" + xml + "' > \"$out\"\n" +
		"printf '%s\\n' '" + restoreLine + "' > '" + restorePath + "'\n" +
		"exit 0\n"
	path := filepath.Join(root, "fake_gtest_restore.sh")
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
