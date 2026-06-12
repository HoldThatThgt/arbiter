package match

import (
	"context"
	"encoding/json"
	"sync"
	"testing"

	"github.com/HoldThatThgt/arbiter/internal/engineclient"
	"github.com/HoldThatThgt/arbiter/internal/verify"
)

const goalEngineBook = `---
name: run-goal-cache
description: async run goal engine cache
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

// fakeExecEngine is a counting in-process stand-in for the Python exec
// engine: RunStatus pops successive states, terminal states carry result.
type fakeExecEngine struct {
	mu       sync.Mutex
	states   []string
	result   string
	poisoned bool
	closed   bool
	starts   int
	statuses int
	respawns int
}

func (f *fakeExecEngine) StartRun(ctx context.Context, spec, meta any) (engineclient.RunStart, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.starts++
	return engineclient.RunStart{RunID: "run-1", State: "running"}, nil
}

func (f *fakeExecEngine) RunStatus(ctx context.Context, runID string) (engineclient.RunStatus, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.statuses++
	state := f.states[0]
	if len(f.states) > 1 {
		f.states = f.states[1:]
	}
	status := engineclient.RunStatus{RunID: runID, State: state}
	if state == "completed" || state == "failed" {
		status.Result = json.RawMessage(f.result)
	}
	return status, nil
}

func (f *fakeExecEngine) Poisoned() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.poisoned
}

func (f *fakeExecEngine) Respawn(ctx context.Context) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.respawns++
	f.poisoned = false
	return nil
}

func (f *fakeExecEngine) Close() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.closed = true
	return nil
}

func (f *fakeExecEngine) snapshot() (starts, statuses, respawns int, closed bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.starts, f.statuses, f.respawns, f.closed
}

// runGoalCacheStore wires a match whose goal is an async run, backed by the
// counting fake engine instead of a real Python child.
func runGoalCacheStore(t *testing.T, fake *fakeExecEngine) (*Store, *int) {
	t.Helper()
	root := repoWithBook(t, "run.md", goalEngineBook)
	writeRecipes(t, root, "unit", "harness:\n  kind: gtest\ntest_run:\n  cmd: [./fake-gtest]\n")
	store := New(root, "test")
	spawns := 0
	store.spawnExec = func(ctx context.Context, root string) (execEngine, error) {
		spawns++
		return fake, nil
	}
	if _, err := store.LoadPlayBook("run-goal-cache"); err != nil {
		t.Fatal(err)
	}
	task, err := store.CreateTask("work")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.SubmitTask(context.Background(), task.TaskID, "done", "done", verify.ResultSpec{Kind: "shell", Command: "exit 0"}); err != nil {
		t.Fatal(err)
	}
	return store, &spawns
}

func (s *Store) cachedGoalEngine() execEngine {
	s.engineMu.Lock()
	defer s.engineMu.Unlock()
	return s.goalEngine
}

// 两次连续 poll 必须复用同一个引擎子进程(只 spawn 一次),settle 后关闭并清空缓存。
func TestGoalPollsReuseOneEngineAndSettleClosesIt(t *testing.T) {
	fake := &fakeExecEngine{
		states: []string{"running", "completed"},
		result: `{"overall":"passed","passed":1,"failed":0,"test_results":{"Suite.Case":"passed"}}`,
	}
	store, spawns := runGoalCacheStore(t, fake)

	started, err := store.CheckStepJob(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if started.Reason != "goal_running" || started.RunID == "" {
		t.Fatalf("started = %#v", started)
	}
	polled, err := store.CheckStepJob(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if polled.Reason != "goal_running" {
		t.Fatalf("polled = %#v", polled)
	}
	if *spawns != 1 {
		t.Fatalf("spawns after start+poll = %d, want 1 (engine must be reused)", *spawns)
	}

	settled, err := store.CheckStepJob(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !settled.Checkmate || settled.Goal == nil || settled.Goal.Verdict != TaskPass {
		t.Fatalf("settled = %#v", settled)
	}
	starts, statuses, _, closed := fake.snapshot()
	if *spawns != 1 || starts != 1 || statuses != 2 {
		t.Fatalf("spawns=%d starts=%d statuses=%d, want 1/1/2", *spawns, starts, statuses)
	}
	if !closed {
		t.Fatal("settle must close the cached engine")
	}
	if store.cachedGoalEngine() != nil {
		t.Fatal("settle must clear the cached engine")
	}
}

// 中毒的缓存引擎在下一次 poll 前被原地重生(respawnIfPoisoned 模式),绝不 fatal。
func TestGoalPollRespawnsPoisonedCachedEngine(t *testing.T) {
	fake := &fakeExecEngine{
		states: []string{"running", "running"},
	}
	store, spawns := runGoalCacheStore(t, fake)

	if _, err := store.CheckStepJob(context.Background()); err != nil {
		t.Fatal(err)
	}
	fake.mu.Lock()
	fake.poisoned = true
	fake.mu.Unlock()

	polled, err := store.CheckStepJob(context.Background())
	if err != nil {
		t.Fatalf("poll with poisoned cached engine must respawn, got %v", err)
	}
	if polled.Reason != "goal_running" {
		t.Fatalf("polled = %#v", polled)
	}
	_, _, respawns, closed := fake.snapshot()
	if respawns != 1 || closed || *spawns != 1 {
		t.Fatalf("respawns=%d closed=%v spawns=%d, want 1/false/1", respawns, closed, *spawns)
	}
}

// state_changed 弃置 pending(回合号不一致)时,缓存引擎必须随之关闭。
func TestStalePendingDiscardClosesCachedEngine(t *testing.T) {
	fake := &fakeExecEngine{states: []string{"running"}}
	store, spawns := runGoalCacheStore(t, fake)

	started, err := store.CheckStepJob(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if started.Reason != "goal_running" {
		t.Fatalf("started = %#v", started)
	}
	if _, err := store.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.GoalPending == nil {
			t.Fatal("missing goal pending")
		}
		m.GoalPending.RoundSeq = m.RoundSeq + 1 // 伪造一个属于已死回合的 pending
		return m, nil, nil
	}); err != nil {
		t.Fatal(err)
	}

	out, err := store.CheckStepJob(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if out.Reason != "state_changed" {
		t.Fatalf("out = %#v", out)
	}
	_, _, _, closed := fake.snapshot()
	if !closed {
		t.Fatal("discarding the pending must close the cached engine")
	}
	if store.cachedGoalEngine() != nil {
		t.Fatal("discarding the pending must clear the cached engine")
	}
	if *spawns != 1 {
		t.Fatalf("spawns = %d, want 1", *spawns)
	}
}

// CloseEngines 关闭 Store 持有的缓存引擎(宿主 shutdown 钩子)。
func TestCloseEnginesClosesCachedEngine(t *testing.T) {
	fake := &fakeExecEngine{states: []string{"running"}}
	store, _ := runGoalCacheStore(t, fake)
	if _, err := store.CheckStepJob(context.Background()); err != nil {
		t.Fatal(err)
	}
	store.CloseEngines()
	if _, _, _, closed := fake.snapshot(); !closed {
		t.Fatal("CloseEngines must close the cached engine")
	}
	if store.cachedGoalEngine() != nil {
		t.Fatal("CloseEngines must clear the cached engine")
	}
}
