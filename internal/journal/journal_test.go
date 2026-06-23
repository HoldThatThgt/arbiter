package journal

import (
	"os"
	"path/filepath"
	"testing"
)

// TestAppendMode0600 asserts the referee journal is created owner-only (0600).
// ADR-0008, design.md:161 and go-referee.md:97 all require 0600 for the
// full-fidelity forensics journal.
func TestAppendMode0600(t *testing.T) {
	root := t.TempDir()
	if err := Append(root, "referee", "test_event", map[string]any{"k": "v"}); err != nil {
		t.Fatalf("Append: %v", err)
	}
	path := filepath.Join(root, ".arbiter", "match", "log", "journal.jsonl")
	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat journal: %v", err)
	}
	if got := info.Mode().Perm(); got != 0o600 {
		t.Fatalf("journal mode = %o, want 0600", got)
	}
}
