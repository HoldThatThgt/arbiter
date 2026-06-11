package shared

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestLockInventoryPaths(t *testing.T) {
	root := t.TempDir()

	if got, want := Path(root, MatchLock), filepath.Join(root, ".arbiter", "locks", "match.lock"); got != want {
		t.Fatalf("match path = %q want %q", got, want)
	}
	build := BuildLock(root)
	if !strings.HasPrefix(build.Label, "build/") || !strings.HasSuffix(build.Label, ".lock") {
		t.Fatalf("build label = %q", build.Label)
	}
	if got, want := Path(root, build), filepath.Join(root, ".arbiter", "locks", "build", build.Key+".lock"); got != want {
		t.Fatalf("build path = %q want %q", got, want)
	}
}

func TestLockOrderViolation(t *testing.T) {
	root := t.TempDir()

	_, err := Acquire(root, []LockSpec{StateLock, SnapshotLock}, 10*time.Millisecond)
	if err == nil || !strings.Contains(err.Error(), "lock order") {
		t.Fatalf("order err = %v", err)
	}
}

func TestLockTimeout(t *testing.T) {
	root := t.TempDir()
	first, err := Acquire(root, []LockSpec{StateLock}, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer first.Release()

	start := time.Now()
	second, err := Acquire(root, []LockSpec{StateLock}, 20*time.Millisecond)
	if second != nil {
		second.Release()
	}
	if err == nil {
		t.Fatal("expected timeout")
	}
	var timeout *TimeoutError
	if !AsTimeout(err, &timeout) || timeout.Lock != "state.lock" {
		t.Fatalf("timeout = %#v err=%v", timeout, err)
	}
	if time.Since(start) > 500*time.Millisecond {
		t.Fatalf("timeout took too long: %s", time.Since(start))
	}
}

func TestNoAdHocFlocksOutsideShared(t *testing.T) {
	root := filepath.Clean(filepath.Join("..", ".."))
	var offenders []string
	for _, dir := range []string{"internal", "cmd"} {
		err := filepath.WalkDir(filepath.Join(root, dir), func(path string, d os.DirEntry, err error) error {
			if err != nil {
				return err
			}
			if d.IsDir() || !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
				return nil
			}
			rel, err := filepath.Rel(root, path)
			if err != nil {
				return err
			}
			if rel == filepath.Join("internal", "shared", "locks.go") {
				return nil
			}
			data, err := os.ReadFile(path)
			if err != nil {
				return err
			}
			text := string(data)
			if strings.Contains(text, "syscall.Flock") || strings.Contains(text, "LOCK_EX") || strings.Contains(text, "LOCK_NB") {
				offenders = append(offenders, rel)
			}
			return nil
		})
		if err != nil {
			t.Fatal(err)
		}
	}
	if len(offenders) != 0 {
		t.Fatalf("ad hoc flocks: %v", offenders)
	}
}
