package cli

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
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
	if status.Runs.Rows != 0 {
		t.Fatalf("runs = %#v", status.Runs)
	}
}

func TestStatusCountsAsyncRuns(t *testing.T) {
	root := t.TempDir()
	writeRunDB(t, filepath.Join(root, ".arbiter", "runs", "state.sqlite"))

	status, err := Status(root)
	if err != nil {
		t.Fatal(err)
	}
	if status.Runs.Rows != 2 {
		t.Fatalf("runs = %#v, want 2 rows", status.Runs)
	}
}

func TestStatusDegradesRunsWhenPythonUnavailable(t *testing.T) {
	root := t.TempDir()
	// Match + facts are pure file reads with no python dependency.
	writeJSON(t, filepath.Join(root, ".arbiter", "match", "status.json"), map[string]any{
		"match_id": "m1",
		"status":   "active",
	})
	writeJSON(t, filepath.Join(root, ".arbiter", "facts", "snapshots", "current", "manifest.json"), map[string]any{
		"snapshot_id": "s1",
		"files":       []string{"src/a.c"},
	})
	// A runs DB exists, so readRunCount would shell out to python; point the
	// interpreter at a binary that does not exist so the invocation fails.
	writeRunDB(t, filepath.Join(root, ".arbiter", "runs", "state.sqlite"))
	t.Setenv("ARBITER_ENGINE_PYTHON", filepath.Join(root, "no-such-python3"))

	status, err := Status(root)
	if err != nil {
		t.Fatalf("Status must degrade the runs subsystem, not error: %v", err)
	}
	if status.Match["status"] != "active" {
		t.Fatalf("match = %#v", status.Match)
	}
	if !status.Facts.Published || status.Facts.SnapshotID != "s1" {
		t.Fatalf("facts = %#v", status.Facts)
	}
	if status.Runs.Available {
		t.Fatalf("runs should be unavailable when python fails: %#v", status.Runs)
	}
	if status.Runs.Rows != 0 {
		t.Fatalf("runs should degrade to 0 rows: %#v", status.Runs)
	}
	if want := "runs=unavailable"; !strings.Contains(FormatStatus(status), want) {
		t.Fatalf("FormatStatus = %q, want substring %q", FormatStatus(status), want)
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

	// The unrelated run r2 exists in async_runs but is outside match m1's
	// journal, so the match-scoped report must exclude it.
	if report.MatchID != "m1" || len(report.Runs) != 1 {
		t.Fatalf("report = %#v", report)
	}
	row := report.Runs[0]
	if row.RunID != "r1" || row.Overall != "failed" || row.State != "completed" {
		t.Fatalf("runs = %#v", report.Runs)
	}
	if row.TargetID != "unit" || row.Profile != "debug" {
		t.Fatalf("runs = %#v", report.Runs)
	}
	if len(report.TaskRuns) != 1 || report.TaskRuns[0].TaskID != "T1" || report.TaskRuns[0].Overall != "failed" {
		t.Fatalf("task runs = %#v", report.TaskRuns)
	}
}

func TestReportWithoutMatchListsAllAsyncRuns(t *testing.T) {
	root := t.TempDir()
	writeRunDB(t, filepath.Join(root, ".arbiter", "runs", "state.sqlite"))

	report, err := Report(root, "")
	if err != nil {
		t.Fatal(err)
	}
	if len(report.Runs) != 2 || report.Runs[0].RunID != "r1" || report.Runs[1].RunID != "r2" {
		t.Fatalf("runs = %#v", report.Runs)
	}
	if report.Runs[1].State != "running" || report.Runs[1].Overall != "" {
		t.Fatalf("runs = %#v", report.Runs)
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

// writeRunDB seeds the async_runs schema exactly as the engine creates it
// (engine/arbiter_engine/runs/state.py _create_schema, written by
// engine/arbiter_engine/runs/async_runs.py): r1 is a finished recipe run for
// match m1's journal, r2 is an unrelated still-running run.
func writeRunDB(t *testing.T, path string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	script := `
import json, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.executescript("""
CREATE TABLE async_runs (
  run_id TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  spec_json TEXT NOT NULL,
  result_json TEXT,
  worker_pid INTEGER,
  started_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
""")
spec = {"kind": "run", "recipe": "unit", "options": {"profiles": ["debug"]}}
result = {"overall": "failed"}
conn.execute(
    "INSERT INTO async_runs VALUES (?,?,?,?,?,?,?)",
    ("r1", "completed", json.dumps(spec), json.dumps(result), None, 1.0, 2.0),
)
conn.execute(
    "INSERT INTO async_runs VALUES (?,?,?,?,?,?,?)",
    ("r2", "running", json.dumps({"kind": "stub"}), None, 4242, 3.0, 3.0),
)
conn.commit()
`
	cmd := exec.Command("python3", "-c", script, path)
	if output, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("sqlite fixture: %v\n%s", err, output)
	}
}
