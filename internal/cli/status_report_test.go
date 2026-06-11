package cli

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

func TestStatusComposesOnRead(t *testing.T) {
	root := t.TempDir()
	writeJSON(t, filepath.Join(root, ".arbiter", "match", "status.json"), map[string]any{
		"match_id": "m1",
		"status":   "active",
		"round":    2,
	})
	writeJSON(t, filepath.Join(root, ".arbiter", "run", "engines.json"), map[string]any{
		"python":         "/usr/bin/python3",
		"engine_version": "test-engine",
	})
	writeJSON(t, filepath.Join(root, ".arbiter", "facts", "snapshots", "current", "manifest.json"), map[string]any{
		"snapshot_id": "s1",
		"files":       []string{"src/a.c"},
	})

	status, err := Status(root)
	if err != nil {
		t.Fatal(err)
	}

	if status.Match["status"] != "active" || status.Match["match_id"] != "m1" {
		t.Fatalf("match = %#v", status.Match)
	}
	if !status.Engine.Configured || status.Engine.Version != "test-engine" {
		t.Fatalf("engine = %#v", status.Engine)
	}
	if !status.Facts.Published || status.Facts.SnapshotID != "s1" || status.Facts.Files != 1 {
		t.Fatalf("facts = %#v", status.Facts)
	}
}

func TestReportJoinsJournalRuns(t *testing.T) {
	root := t.TempDir()
	writeJSONL(t, filepath.Join(root, ".arbiter", "match", "log", "journal.jsonl"),
		map[string]any{"event": "task_submitted", "match_id": "m1", "round": 1, "task": "T1", "run_id": "r1"},
	)
	writeRunDB(t, filepath.Join(root, ".arbiter", "runs", "state.sqlite"))

	report, err := Report(root, "m1")
	if err != nil {
		t.Fatal(err)
	}

	if report.MatchID != "m1" || len(report.Runs) != 1 {
		t.Fatalf("report = %#v", report)
	}
	if report.Runs[0].RunID != "r1" || report.Runs[0].Overall != "failed" {
		t.Fatalf("runs = %#v", report.Runs)
	}
	if len(report.TaskRuns) != 1 || report.TaskRuns[0].TaskID != "T1" || report.TaskRuns[0].Overall != "failed" {
		t.Fatalf("task runs = %#v", report.TaskRuns)
	}
}

func writeJSON(t *testing.T, path string, value any) {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, append(data, '\n'), 0o644); err != nil {
		t.Fatal(err)
	}
}

func writeJSONL(t *testing.T, path string, values ...map[string]any) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	var data []byte
	for _, value := range values {
		line, err := json.Marshal(value)
		if err != nil {
			t.Fatal(err)
		}
		data = append(data, line...)
		data = append(data, '\n')
	}
	if err := os.WriteFile(path, data, 0o644); err != nil {
		t.Fatal(err)
	}
}

func writeRunDB(t *testing.T, path string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	script := `
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.executescript("""
CREATE TABLE run (
  run_id TEXT PRIMARY KEY,
  match_id TEXT,
  task_id TEXT,
  round INTEGER,
  target_id TEXT NOT NULL,
  profile TEXT NOT NULL,
  state TEXT NOT NULL,
  overall TEXT,
  started_at REAL NOT NULL,
  finished_at REAL
);
INSERT INTO run VALUES ('r1','m1','T1',1,'unit','debug','completed','failed',1.0,2.0);
""")
conn.commit()
`
	cmd := exec.Command("python3", "-c", script, path)
	if output, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("sqlite fixture: %v\n%s", err, output)
	}
}
