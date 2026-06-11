package match

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/HoldThatThgt/arbiter/internal/shared"
)

func TestStoreUsesInventoryMatchLock(t *testing.T) {
	root := repoWithBook(t, "flow.md", twoStepBook)
	store := New(root, "test")

	held, err := shared.Acquire(root, []shared.LockSpec{shared.MatchLock}, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer held.Release()

	_, err = store.LoadPlayBook("flow")
	if err == nil {
		t.Fatal("expected lock timeout")
	}
	if toolErr, ok := err.(*ToolError); !ok || toolErr.Code != "lock_timeout" {
		t.Fatalf("err = %#v", err)
	}
	if _, err := os.Stat(filepath.Join(root, ".arbiter", "match", "run", "lock")); !os.IsNotExist(err) {
		t.Fatalf("legacy lock path exists or stat failed: %v", err)
	}
}
