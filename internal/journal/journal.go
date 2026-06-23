package journal

import (
	"encoding/json"
	"os"
	"path/filepath"
	"time"

	"github.com/HoldThatThgt/arbiter/internal/shared"
)

type Event map[string]any

func Append(root, seat, event string, fields map[string]any) error {
	if fields == nil {
		fields = map[string]any{}
	}
	fields["ts"] = time.Now().UTC().Format(time.RFC3339)
	fields["seat"] = seat
	fields["event"] = event

	dir := filepath.Join(root, ".arbiter", "match", "log")
	path := filepath.Join(dir, "journal.jsonl")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	enc, err := json.Marshal(fields)
	if err != nil {
		return err
	}
	if _, err := f.Write(append(enc, '\n')); err != nil {
		return err
	}
	if err := f.Sync(); err != nil {
		return err
	}
	// Best-effort parent-dir fsync so a newly-created journal file's
	// directory entry is crash-durable (matches the "fsync'd" doc claim).
	shared.SyncDir(dir)
	return nil
}
