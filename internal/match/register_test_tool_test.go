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
