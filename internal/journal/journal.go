package journal

import (
	"encoding/json"
	"os"
	"path/filepath"
	"time"
)

type Event map[string]any

func Append(root, seat, event string, fields map[string]any) error {
	if fields == nil {
		fields = map[string]any{}
	}
	fields["ts"] = time.Now().UTC().Format(time.RFC3339)
	fields["seat"] = seat
	fields["event"] = event

	path := filepath.Join(root, ".arbiter", "match", "log", "journal.jsonl")
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
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
	return f.Sync()
}
