package match

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/HoldThatThgt/arbiter/internal/journal"
	"github.com/HoldThatThgt/arbiter/internal/playbook"
	"github.com/HoldThatThgt/arbiter/internal/shared"
)

type Store struct {
	Root string
	Seat string
}

func New(root, seat string) *Store {
	return &Store{Root: root, Seat: seat}
}

func (s *Store) statePath() string {
	return filepath.Join(s.Root, ".arbiter", "match", "run", "state.json")
}

func (s *Store) statusPath() string {
	return filepath.Join(s.Root, ".arbiter", "match", "status.json")
}

func (s *Store) lockPath() string {
	return shared.Path(s.Root, shared.MatchLock)
}

func (s *Store) playbookDir() string {
	return filepath.Join(s.Root, ".arbiter", "playbook")
}

func (s *Store) withLock(fn func(*Match) (*Match, any, error)) (any, error) {
	unlock, err := s.lock()
	if err != nil {
		return nil, err
	}
	defer unlock()

	current, err := s.readState()
	if err != nil {
		return nil, err
	}
	next, out, err := fn(current)
	if err != nil {
		return nil, err
	}
	if next != nil {
		if err := s.writeState(next); err != nil {
			return nil, &ToolError{Code: playbook.CodeStateCorrupt, Message: err.Error()}
		}
	}
	return out, nil
}

func (s *Store) lock() (func(), error) {
	held, err := shared.Acquire(s.Root, []shared.LockSpec{shared.MatchLock}, time.Duration(playbook.LockTimeoutS)*time.Second)
	if err != nil {
		var timeout *shared.TimeoutError
		if shared.AsTimeout(err, &timeout) {
			return nil, &ToolError{
				Code:    playbook.CodeLockTimeout,
				Message: "lock timeout",
				Data:    map[string]any{"lock": timeout.Lock},
			}
		}
		return nil, err
	}
	return held.Release, nil
}

func (s *Store) readState() (*Match, error) {
	data, err := os.ReadFile(s.statePath())
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, &ToolError{Code: playbook.CodeStateCorrupt, Message: err.Error()}
	}
	var m Match
	if err := json.Unmarshal(data, &m); err != nil {
		return nil, &ToolError{Code: playbook.CodeStateCorrupt, Message: err.Error()}
	}
	return &m, nil
}

func (s *Store) writeState(m *Match) error {
	if err := atomicJSON(s.statePath(), m, 0o600); err != nil {
		return err
	}
	return atomicJSON(s.statusPath(), projectStatus(m), 0o644)
}

func atomicJSON(path string, value any, perm os.FileMode) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	return atomicFile(path, append(data, '\n'), perm)
}

func atomicFile(path string, data []byte, perm os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".tmp-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	ok := false
	defer func() {
		if !ok {
			_ = os.Remove(tmpName)
		}
	}()
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Chmod(perm); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmpName, path); err != nil {
		return err
	}
	ok = true
	return nil
}

func (s *Store) append(event string, fields map[string]any) {
	_ = journal.Append(s.Root, s.Seat, event, fields)
}

func newMatchID(now time.Time) string {
	var b [2]byte
	if _, err := rand.Read(b[:]); err != nil {
		copy(b[:], []byte{0, 0})
	}
	return fmt.Sprintf("m-%s-%s", now.UTC().Format("20060102T150405Z"), hex.EncodeToString(b[:]))
}

func utcNow() string {
	return time.Now().UTC().Format(time.RFC3339)
}
