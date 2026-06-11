package shared

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"time"
)

type LockSpec struct {
	Name  string
	Label string
	Key   string
	order int
}

var (
	MatchLock    = LockSpec{Name: "match", Label: "match.lock", order: 10}
	SnapshotLock = LockSpec{Name: "snapshot", Label: "snapshot.lock", order: 20}
	OverlayLock  = LockSpec{Name: "overlay", Label: "overlay.lock", order: 30}
	StateLock    = LockSpec{Name: "state", Label: "state.lock", order: 40}
)

type TimeoutError struct {
	Lock string
}

func (e *TimeoutError) Error() string {
	return "lock_timeout: " + e.Lock
}

func AsTimeout(err error, target **TimeoutError) bool {
	return errors.As(err, target)
}

type HeldLocks struct {
	files []*os.File
}

func BuildLock(workdir string) LockSpec {
	abs, err := filepath.Abs(workdir)
	if err != nil {
		abs = workdir
	}
	sum := sha256.Sum256([]byte(abs))
	key := hex.EncodeToString(sum[:])[:8]
	return LockSpec{Name: "build", Label: filepath.ToSlash(filepath.Join("build", key+".lock")), Key: key, order: 50}
}

func Path(root string, spec LockSpec) string {
	base := filepath.Join(root, ".arbiter", "locks")
	if spec.Name == "build" {
		return filepath.Join(base, "build", spec.Key+".lock")
	}
	return filepath.Join(base, spec.Label)
}

func Acquire(root string, specs []LockSpec, timeout time.Duration) (*HeldLocks, error) {
	if err := assertOrder(specs); err != nil {
		return nil, err
	}
	held := &HeldLocks{}
	deadline := time.Now().Add(timeout)
	for _, spec := range specs {
		path := Path(root, spec)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			held.Release()
			return nil, err
		}
		f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
		if err != nil {
			held.Release()
			return nil, err
		}
		for {
			err = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
			if err == nil {
				held.files = append(held.files, f)
				break
			}
			if time.Now().After(deadline) {
				_ = f.Close()
				held.Release()
				return nil, &TimeoutError{Lock: spec.Label}
			}
			time.Sleep(10 * time.Millisecond)
		}
	}
	return held, nil
}

func (h *HeldLocks) Release() {
	for i := len(h.files) - 1; i >= 0; i-- {
		_ = syscall.Flock(int(h.files[i].Fd()), syscall.LOCK_UN)
		_ = h.files[i].Close()
	}
	h.files = nil
}

func assertOrder(specs []LockSpec) error {
	previous := 0
	for _, spec := range specs {
		if spec.order <= previous {
			return fmt.Errorf("lock order violation")
		}
		previous = spec.order
	}
	return nil
}
