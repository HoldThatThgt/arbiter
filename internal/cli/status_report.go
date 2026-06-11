package cli

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

const (
	StatusSchema = "arbiter.status.v1"
	ReportSchema = "arbiter.report.v1"
)

type StatusResult struct {
	Schema string         `json:"schema"`
	Match  map[string]any `json:"match"`
	Engine EngineStatus   `json:"engine"`
	Facts  FactsStatus    `json:"facts"`
	Runs   RunsStatus     `json:"runs"`
}

type EngineStatus struct {
	Configured bool   `json:"configured"`
	Python     string `json:"python,omitempty"`
	Version    string `json:"version,omitempty"`
	Mode       string `json:"mode,omitempty"`
}

type FactsStatus struct {
	Published  bool   `json:"published"`
	SnapshotID string `json:"snapshot_id,omitempty"`
	Files      int    `json:"files"`
}

type RunsStatus struct {
	Rows int `json:"rows"`
}

type ReportResult struct {
	Schema   string           `json:"schema"`
	MatchID  string           `json:"match_id,omitempty"`
	Events   []map[string]any `json:"events"`
	Runs     []RunRow         `json:"runs"`
	TaskRuns []TaskRun        `json:"task_runs"`
}

type RunRow struct {
	RunID    string `json:"run_id"`
	MatchID  string `json:"match_id,omitempty"`
	TaskID   string `json:"task_id,omitempty"`
	Round    int    `json:"round,omitempty"`
	TargetID string `json:"target_id"`
	Profile  string `json:"profile"`
	State    string `json:"state"`
	Overall  string `json:"overall,omitempty"`
}

type TaskRun struct {
	TaskID  string `json:"task_id"`
	RunID   string `json:"run_id"`
	Overall string `json:"overall,omitempty"`
	State   string `json:"state"`
}

func Status(root string) (StatusResult, error) {
	match, err := readJSONObject(filepath.Join(root, ".arbiter", "match", "status.json"))
	if err != nil {
		return StatusResult{}, err
	}
	if match == nil {
		match = map[string]any{"status": "absent"}
	}
	runs, err := readRunRows(root, "")
	if err != nil {
		return StatusResult{}, err
	}
	return StatusResult{
		Schema: StatusSchema,
		Match:  match,
		Engine: readEngineStatus(root),
		Facts:  readFactsStatus(root),
		Runs:   RunsStatus{Rows: len(runs)},
	}, nil
}

func Report(root, matchID string) (ReportResult, error) {
	events, err := readJournal(filepath.Join(root, ".arbiter", "match", "log", "journal.jsonl"), matchID)
	if err != nil {
		return ReportResult{}, err
	}
	runs, err := readRunRows(root, matchID)
	if err != nil {
		return ReportResult{}, err
	}
	byRunID := map[string]RunRow{}
	for _, row := range runs {
		byRunID[row.RunID] = row
	}
	var taskRuns []TaskRun
	for _, event := range events {
		runID, _ := event["run_id"].(string)
		if runID == "" {
			continue
		}
		row, ok := byRunID[runID]
		if !ok {
			continue
		}
		taskID, _ := event["task"].(string)
		if taskID == "" {
			taskID, _ = event["task_id"].(string)
		}
		if taskID == "" {
			taskID = row.TaskID
		}
		taskRuns = append(taskRuns, TaskRun{
			TaskID:  taskID,
			RunID:   runID,
			Overall: row.Overall,
			State:   row.State,
		})
	}
	return ReportResult{
		Schema:   ReportSchema,
		MatchID:  matchID,
		Events:   events,
		Runs:     runs,
		TaskRuns: taskRuns,
	}, nil
}

func FormatStatus(status StatusResult) string {
	matchStatus, _ := status.Match["status"].(string)
	if matchStatus == "" {
		matchStatus = "unknown"
	}
	snapshot := status.Facts.SnapshotID
	if snapshot == "" {
		snapshot = "none"
	}
	return fmt.Sprintf("match=%s facts.published=%t snapshot=%s runs=%d engine=%s\n",
		matchStatus, status.Facts.Published, snapshot, status.Runs.Rows, status.Engine.Version)
}

func FormatReport(report ReportResult) string {
	return fmt.Sprintf("match=%s events=%d runs=%d task_runs=%d\n", report.MatchID, len(report.Events), len(report.Runs), len(report.TaskRuns))
}

func readEngineStatus(root string) EngineStatus {
	raw, err := readJSONObject(filepath.Join(root, ".arbiter", "run", "engines.json"))
	if err != nil || raw == nil {
		return EngineStatus{}
	}
	status := EngineStatus{Configured: true}
	status.Python, _ = raw["python"].(string)
	status.Version, _ = raw["engine_version"].(string)
	status.Mode, _ = raw["mode"].(string)
	return status
}

func readFactsStatus(root string) FactsStatus {
	raw, err := readJSONObject(filepath.Join(root, ".arbiter", "facts", "snapshots", "current", "manifest.json"))
	if err != nil || raw == nil {
		return FactsStatus{}
	}
	status := FactsStatus{Published: true}
	status.SnapshotID, _ = raw["snapshot_id"].(string)
	if files, ok := raw["files"].([]any); ok {
		status.Files = len(files)
	}
	return status
}

func readJSONObject(path string) (map[string]any, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, err
	}
	return out, nil
}

func readJournal(path, matchID string) ([]map[string]any, error) {
	file, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	defer file.Close()
	var events []map[string]any
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		var event map[string]any
		if err := json.Unmarshal(scanner.Bytes(), &event); err != nil {
			continue
		}
		if matchID != "" {
			found, _ := event["match_id"].(string)
			if found != matchID {
				continue
			}
		}
		events = append(events, event)
	}
	return events, scanner.Err()
}

func readRunRows(root, matchID string) ([]RunRow, error) {
	dbPath := filepath.Join(root, ".arbiter", "runs", "state.sqlite")
	if _, err := os.Stat(dbPath); errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	script := `
import json, os, sqlite3, sys
db, match_id = sys.argv[1], sys.argv[2]
if not os.path.exists(db):
    print("[]")
    raise SystemExit(0)
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
try:
    if match_id:
        rows = conn.execute("SELECT run_id, match_id, task_id, round, target_id, profile, state, overall FROM run WHERE match_id = ? ORDER BY started_at, run_id", (match_id,)).fetchall()
    else:
        rows = conn.execute("SELECT run_id, match_id, task_id, round, target_id, profile, state, overall FROM run ORDER BY started_at, run_id").fetchall()
except sqlite3.Error:
    rows = []
print(json.dumps([dict(row) for row in rows], separators=(",", ":")))
`
	cmd := exec.Command(pythonBin(), "-c", script, dbPath, matchID)
	output, err := cmd.Output()
	if err != nil {
		return nil, err
	}
	var raw []map[string]any
	if err := json.Unmarshal(output, &raw); err != nil {
		return nil, err
	}
	rows := make([]RunRow, 0, len(raw))
	for _, item := range raw {
		rows = append(rows, RunRow{
			RunID:    stringValue(item["run_id"]),
			MatchID:  stringValue(item["match_id"]),
			TaskID:   stringValue(item["task_id"]),
			Round:    intValue(item["round"]),
			TargetID: stringValue(item["target_id"]),
			Profile:  stringValue(item["profile"]),
			State:    stringValue(item["state"]),
			Overall:  stringValue(item["overall"]),
		})
	}
	return rows, nil
}

func pythonBin() string {
	if python := os.Getenv("PYTHON"); python != "" {
		return python
	}
	return "python3"
}

func stringValue(value any) string {
	if value == nil {
		return ""
	}
	text, _ := value.(string)
	return text
}

func intValue(value any) int {
	switch n := value.(type) {
	case float64:
		return int(n)
	case int:
		return n
	default:
		return 0
	}
}

func JSON(value any) ([]byte, error) {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return nil, err
	}
	return append(data, '\n'), nil
}

func ParseReportArgs(args []string) (bool, string, error) {
	jsonOut := false
	var rest []string
	for _, arg := range args {
		if arg == "--json" {
			jsonOut = true
			continue
		}
		if strings.HasPrefix(arg, "-") {
			return false, "", fmt.Errorf("unknown flag %s", arg)
		}
		rest = append(rest, arg)
	}
	if len(rest) > 1 {
		return false, "", fmt.Errorf("usage")
	}
	matchID := ""
	if len(rest) == 1 {
		matchID = rest[0]
	}
	return jsonOut, matchID, nil
}
