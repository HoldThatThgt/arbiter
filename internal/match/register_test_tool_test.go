package match

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
	"github.com/HoldThatThgt/arbiter/internal/verify"
)

// A registered test is immutable: once frozen, any modification to it makes
// every subsequent SubmitTask fail before the predicate even runs — so a
// tampered test can never produce a pass, no matter how trivially-true the
// predicate. An unmodified frozen test is invisible: predicates run normally.
func TestRegisterTestFreezesAndDetectsTampering(t *testing.T) {
	root := repoWithBook(t, "flow.md", twoStepBook)
	testPath := filepath.Join(root, "tests", "repro_test.cc")
	if err := os.MkdirAll(filepath.Dir(testPath), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(testPath, []byte("ORIGINAL\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	store := New(root, "test")
	if _, err := store.LoadPlayBook("flow"); err != nil {
		t.Fatal(err)
	}

	// Freeze it.
	out, err := store.RegisterTest([]string{"tests/repro_test.cc"})
	if err != nil {
		t.Fatal(err)
	}
	if len(out.Frozen) != 1 || out.Frozen[0].Path != "tests/repro_test.cc" {
		t.Fatalf("frozen = %#v", out.Frozen)
	}

	// Re-registering the same bytes is idempotent; different bytes is refused.
	if _, err := store.RegisterTest([]string{"tests/repro_test.cc"}); err != nil {
		t.Fatalf("idempotent re-register: %v", err)
	}
	if err := os.WriteFile(testPath, []byte("CHANGED\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := store.RegisterTest([]string{"tests/repro_test.cc"}); toolCode(err) != playbook.CodeTestRegister {
		t.Fatalf("re-register modified: code = %q, want %q", toolCode(err), playbook.CodeTestRegister)
	}
	if err := os.WriteFile(testPath, []byte("ORIGINAL\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	// Frozen test intact → the predicate runs normally and passes.
	task, err := store.CreateTask("t")
	if err != nil {
		t.Fatal(err)
	}
	pass, err := store.SubmitTask(context.Background(), task.TaskID, "s", "r", verify.ResultSpec{Kind: "shell", Command: "exit 0"})
	if err != nil || pass.Verdict != TaskPass {
		t.Fatalf("intact frozen test: verdict = %q err=%v, want pass", pass.Verdict, err)
	}
	if _, err := store.CheckStepJob(context.Background()); err != nil {
		t.Fatal(err)
	}

	// Tamper with the frozen test, then submit a trivially-true predicate: the
	// integrity gate forces a fail before exit 0 can even run.
	if err := os.WriteFile(testPath, []byte("TAMPERED\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	task2, err := store.CreateTask("t2")
	if err != nil {
		t.Fatal(err)
	}
	got, err := store.SubmitTask(context.Background(), task2.TaskID, "looks fixed, all green", "r", verify.ResultSpec{Kind: "shell", Command: "exit 0"})
	if err != nil {
		t.Fatal(err)
	}
	if got.Verdict != TaskFail || got.Failure != playbook.CodeFrozenTestModified {
		t.Fatalf("tampered frozen test: verdict=%q failure=%q, want fail/%s", got.Verdict, got.Failure, playbook.CodeFrozenTestModified)
	}
}

func TestRegisterTestRejectsBadPaths(t *testing.T) {
	root := repoWithBook(t, "flow.md", twoStepBook)
	store := New(root, "test")
	if _, err := store.LoadPlayBook("flow"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.RegisterTest(nil); toolCode(err) != playbook.CodeTestRegister {
		t.Fatalf("empty: code = %q", toolCode(err))
	}
	if _, err := store.RegisterTest([]string{"../escape.cc"}); toolCode(err) != playbook.CodeTestRegister {
		t.Fatalf("traversal: code = %q", toolCode(err))
	}
	if _, err := store.RegisterTest([]string{"missing.cc"}); toolCode(err) != playbook.CodeTestRegister {
		t.Fatalf("missing file: code = %q", toolCode(err))
	}
}

// A symlink (even one lexically under the repo) must not be freezable: it could
// point the frozen object at an out-of-repo file editable without tripping any gate.
func TestRegisterTestRejectsSymlink(t *testing.T) {
	root := repoWithBook(t, "flow.md", twoStepBook)
	if err := os.MkdirAll(filepath.Join(root, "tests"), 0o755); err != nil {
		t.Fatal(err)
	}
	outside := filepath.Join(t.TempDir(), "outside.cc")
	if err := os.WriteFile(outside, []byte("OUT\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(root, "tests", "link.cc")
	if err := os.Symlink(outside, link); err != nil {
		t.Skipf("symlink unsupported on this platform: %v", err)
	}
	store := New(root, "test")
	if _, err := store.LoadPlayBook("flow"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.RegisterTest([]string{"tests/link.cc"}); toolCode(err) != playbook.CodeTestRegister {
		t.Fatalf("symlink freeze: code = %q, want %q", toolCode(err), playbook.CodeTestRegister)
	}
}

// A predicate's own side effects can tamper a frozen test AFTER the pre-execution
// hash check; the post-execution re-hash must catch "rewrite the test, then run a
// weakened suite" and fail the submit.
func TestSubmitTaskCatchesPredicateThatTampersFrozenTest(t *testing.T) {
	root := repoWithBook(t, "flow.md", twoStepBook)
	testPath := filepath.Join(root, "tests", "repro_test.cc")
	if err := os.MkdirAll(filepath.Dir(testPath), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(testPath, []byte("ORIGINAL\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	store := New(root, "test")
	if _, err := store.LoadPlayBook("flow"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.RegisterTest([]string{"tests/repro_test.cc"}); err != nil {
		t.Fatal(err)
	}
	task, err := store.CreateTask("t")
	if err != nil {
		t.Fatal(err)
	}
	// exit 0 alone would pass — but the predicate first rewrites the frozen test.
	got, err := store.SubmitTask(context.Background(), task.TaskID, "looks fixed", "r",
		verify.ResultSpec{Kind: "shell", Command: "echo TAMPERED > tests/repro_test.cc; exit 0"})
	if err != nil {
		t.Fatal(err)
	}
	if got.Verdict != TaskFail || got.Failure != playbook.CodeFrozenTestModified {
		t.Fatalf("predicate-side-effect tamper: verdict=%q failure=%q, want fail/%s", got.Verdict, got.Failure, playbook.CodeFrozenTestModified)
	}
}

// The goal/checkmate predicate is the verdict that actually declares victory.
// A frozen test tampered after the round's tasks passed must block checkmate,
// even when the goal predicate itself is trivially true.
func TestGoalPathRejectsTamperedFrozenTest(t *testing.T) {
	root := repoWithBook(t, "g.md", goalBook("exit 0"))
	testPath := filepath.Join(root, "tests", "repro_test.cc")
	if err := os.MkdirAll(filepath.Dir(testPath), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(testPath, []byte("ORIGINAL\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	store := New(root, "test")
	if _, err := store.LoadPlayBook("goalflow"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.RegisterTest([]string{"tests/repro_test.cc"}); err != nil {
		t.Fatal(err)
	}
	// Clear the round with the test intact so SubmitTask's own gate passes.
	task, err := store.CreateTask("work")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.SubmitTask(context.Background(), task.TaskID, "done", "done", verify.ResultSpec{Kind: "shell", Command: "exit 0"}); err != nil {
		t.Fatal(err)
	}
	// Tamper the frozen test, THEN adjudicate the goal (exit 0 stands in for a
	// neutered test that now trivially passes). Without the gate it would checkmate.
	if err := os.WriteFile(testPath, []byte("TAMPERED\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	out, err := store.CheckStepJob(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if out.Checkmate || out.Match == StatusFinishedSuccess {
		t.Fatalf("tampered frozen test won via goal path: %#v", out)
	}
	if out.Goal == nil || out.Goal.Failure != playbook.CodeFrozenTestModified {
		t.Fatalf("goal report = %#v, want failure %s", out.Goal, playbook.CodeFrozenTestModified)
	}
}
