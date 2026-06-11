package match

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
	"github.com/HoldThatThgt/arbiter/internal/verify"
)

func TestConcurrentSubmitAndCheck(t *testing.T) {
	root := repoWithBook(t, "flow.md", twoStepBook)
	store := New(root, "test")
	if _, err := store.LoadPlayBook("flow"); err != nil {
		t.Fatal(err)
	}
	var tasks []CreateTaskOutput
	for i := 0; i < 8; i++ {
		task, err := store.CreateTask("task")
		if err != nil {
			t.Fatal(err)
		}
		tasks = append(tasks, task)
	}
	var wg sync.WaitGroup
	for _, task := range tasks {
		task := task
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := store.SubmitTask(context.Background(), task.TaskID, "done in parallel", "done", verify.ResultSpec{Kind: "shell", Command: "exit 0"}); err != nil {
				t.Errorf("submit %s: %v", task.TaskID, err)
			}
		}()
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		deadline := time.Now().Add(3 * time.Second)
		for time.Now().Before(deadline) {
			out, err := store.CheckStepJob(context.Background())
			if err != nil {
				t.Errorf("check: %v", err)
				return
			}
			if out.Complete {
				return
			}
			time.Sleep(10 * time.Millisecond)
		}
	}()
	wg.Wait()
	show, err := store.ShowStepJob()
	if err != nil {
		t.Fatal(err)
	}
	if show.Status != StatusActive || show.Round != 2 {
		t.Fatalf("show = %#v", show)
	}
}

func TestSubmitTaskStale(t *testing.T) {
	root := repoWithBook(t, "flow.md", twoStepBook)
	store := New(root, "test")
	if _, err := store.LoadPlayBook("flow"); err != nil {
		t.Fatal(err)
	}
	task, err := store.CreateTask("one task")
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() {
		_, err := store.SubmitTask(context.Background(), task.TaskID, "slow path", "slow", verify.ResultSpec{Kind: "shell", Command: "sleep 1; exit 0"})
		done <- err
	}()
	if _, err := store.SubmitTask(context.Background(), task.TaskID, "fast path", "fast", verify.ResultSpec{Kind: "shell", Command: "exit 0"}); err != nil {
		t.Fatal(err)
	}
	if out, err := store.CheckStepJob(context.Background()); err != nil || !out.Complete {
		t.Fatalf("check = %#v err=%v", out, err)
	}
	err = <-done
	var toolErr *ToolError
	if !errors.As(err, &toolErr) || toolErr.Code != playbook.CodeTaskStale {
		t.Fatalf("stale err = %#v", err)
	}
}
