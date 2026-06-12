package match

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/HoldThatThgt/arbiter/internal/verify"
)

// TestSubmitTaskActuallyExecutesPredicate proves the verdict is not rubber-
// stamped: a shell predicate with a host side effect (writing a sentinel file)
// must actually run, and its exit code — not the executor's prose — decides
// pass/fail. This is the integrity guarantee behind every task verdict.
func TestSubmitTaskActuallyExecutesPredicate(t *testing.T) {
	root := repoWithBook(t, "flow.md", twoStepBook)
	store := New(root, "test")
	if _, err := store.LoadPlayBook("flow"); err != nil {
		t.Fatal(err)
	}

	sentinel := filepath.Join(t.TempDir(), "ran.proof")

	// exit 0 with a side effect → pass, and the side effect must be on disk.
	task, err := store.CreateTask("prove execution")
	if err != nil {
		t.Fatal(err)
	}
	out, err := store.SubmitTask(context.Background(), task.TaskID, "ok", "ok",
		verify.ResultSpec{Kind: "shell", Command: "echo PROOF > " + sentinel + " && exit 0"})
	if err != nil {
		t.Fatal(err)
	}
	if out.Verdict != TaskPass {
		t.Fatalf("verdict = %q, want pass", out.Verdict)
	}
	data, err := os.ReadFile(sentinel)
	if err != nil || string(data) != "PROOF\n" {
		t.Fatalf("sentinel not written by predicate — execution did not happen: data=%q err=%v", data, err)
	}

	// A non-zero exit is a real fail: the referee counts the exit code, not the
	// summary ("all good" is a lie the exit code overrides).
	store.CheckStepJob(context.Background()) // advance to round 2 to take a fresh task
	task2, err := store.CreateTask("prove fail")
	if err != nil {
		t.Fatal(err)
	}
	out2, err := store.SubmitTask(context.Background(), task2.TaskID, "all good, tests pass", "prose",
		verify.ResultSpec{Kind: "shell", Command: "exit 7"})
	if err != nil {
		t.Fatal(err)
	}
	if out2.Verdict != TaskFail {
		t.Fatalf("verdict = %q, want fail (exit 7 must override the rosy summary)", out2.Verdict)
	}
	if out2.ExitCode == nil || *out2.ExitCode != 7 {
		t.Fatalf("exit code = %v, want 7 captured from the real run", out2.ExitCode)
	}
}
