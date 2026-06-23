package cli

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/HoldThatThgt/arbiter/internal/deploy"
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
	// Available is false when the runs subsystem could not be read (e.g. the
	// sqlite-backed count needs python3 and the interpreter is missing/broken).
	// The runs portion then degrades to Rows=0 rather than failing the whole
	// status compose-on-read.
	Available bool `json:"available"`
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
	// readRunCount degrades (available=false, rows=0) rather than erroring when
	// the runs DB is present but unreadable, so a missing/broken python3 does
	// not abort the otherwise file-based status.
	rows, runsAvailable := readRunCount(root)
	return StatusResult{
		Schema: StatusSchema,
		Match:  match,
		Engine: readEngineStatus(root),
		Facts:  readFactsStatus(root),
		Runs:   RunsStatus{Rows: rows, Available: runsAvailable},
	}, nil
}

func Report(root, matchID string) (ReportResult, error) {
	events, err := readJournal(filepath.Join(root, ".arbiter", "match", "log", "journal.jsonl"), matchID)
	if err != nil {
		return ReportResult{}, err
	}
	runs, err := readRunRows(root)
	if err != nil {
		return ReportResult{}, err
	}
	byRunID := map[string]RunRow{}
	for _, row := range runs {
		byRunID[row.RunID] = row
	}
	var taskRuns []TaskRun
	eventRunIDs := map[string]bool{}
	for _, event := range events {
		runID, _ := event["run_id"].(string)
		if runID == "" {
			continue
		}
		eventRunIDs[runID] = true
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
	if matchID != "" {
		// async_runs has no match column, so scope runs to the match via the
		// journal's run_id references.
		var scoped []RunRow
		for _, row := range runs {
			if eventRunIDs[row.RunID] {
				scoped = append(scoped, row)
			}
		}
		runs = scoped
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
	runs := strconv.Itoa(status.Runs.Rows)
	if !status.Runs.Available {
		runs = "unavailable"
	}
	return fmt.Sprintf("match=%s facts.published=%t snapshot=%s runs=%s engine=%s\n",
		matchStatus, status.Facts.Published, snapshot, runs, status.Engine.Version)
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

func runDBPath(root string) string {
	return filepath.Join(root, ".arbiter", "runs", "state.sqlite")
}

// readRunCount is the lightweight status-path helper: it asks sqlite for a
// COUNT(*) instead of materializing every async run row. It returns the count
// and whether the runs subsystem could be queried at all. An absent DB reports
// (0, true). Only a failure to run the probe — a missing/broken python3 that
// cannot launch, or non-integer output — degrades to (0, false) instead of
// erroring, so the rest of (file-based) status still composes. An in-DB problem
// the probe itself absorbs (corrupt file, missing table) still reads as
// (0, true): the script catches sqlite3.Error and prints 0, matching readRunRows.
func readRunCount(root string) (count int, available bool) {
	dbPath := runDBPath(root)
	if _, err := os.Stat(dbPath); errors.Is(err, os.ErrNotExist) {
		return 0, true
	}
	script := `
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
try:
    row = conn.execute("SELECT COUNT(*) FROM async_runs").fetchone()
    print(row[0] if row else 0)
except sqlite3.Error:
    print(0)
`
	cmd := exec.Command(pythonBin(), "-c", script, dbPath)
	output, err := cmd.Output()
	if err != nil {
		return 0, false
	}
	count, err = strconv.Atoi(strings.TrimSpace(string(output)))
	if err != nil {
		return 0, false
	}
	return count, true
}

// readRunRows reads the async_runs table, which is the table the engine
// actually writes for arbiter/startRun goals (see
// engine/arbiter_engine/runs/async_runs.py). The table carries run_id, state,
// spec_json, and result_json; overall comes from result_json, target/profile
// from the spec's recipe/options where present, and fields the source lacks
// (match, task, round) stay empty — Report correlates runs to a match via
// journal run_ids instead.
func readRunRows(root string) ([]RunRow, error) {
	dbPath := runDBPath(root)
	if _, err := os.Stat(dbPath); errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	script := `
import json, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.row_factory = sqlite3.Row
try:
    rows = conn.execute("SELECT run_id, state, spec_json, result_json FROM async_runs ORDER BY started_at, run_id").fetchall()
except sqlite3.Error:
    rows = []
out = []
for row in rows:
    item = {"run_id": row["run_id"], "state": row["state"]}
    try:
        spec = json.loads(row["spec_json"]) if row["spec_json"] else {}
    except ValueError:
        spec = {}
    if isinstance(spec, dict):
        recipe = spec.get("recipe")
        if isinstance(recipe, str):
            item["target_id"] = recipe
        options = spec.get("options")
        if isinstance(options, dict):
            profiles = options.get("profiles")
            if isinstance(profiles, list) and all(isinstance(p, str) for p in profiles):
                item["profile"] = ",".join(profiles)
    try:
        result = json.loads(row["result_json"]) if row["result_json"] else {}
    except ValueError:
        result = {}
    if isinstance(result, dict):
        overall = result.get("overall")
        if isinstance(overall, str):
            item["overall"] = overall
    out.append(item)
print(json.dumps(out, separators=(",", ":")))
`
	cmd := exec.Command(pythonBin(), "-c", script, dbPath)
	output, err := cmd.Output()
	if err != nil {
		// Mirror readRunCount: a present-but-unreadable runs DB (e.g.
		// missing/broken python3) degrades the runs subsystem to empty rather
		// than aborting Report, whose journal portion is a pure file read.
		return nil, nil
	}
	var raw []map[string]any
	if err := json.Unmarshal(output, &raw); err != nil {
		return nil, nil
	}
	rows := make([]RunRow, 0, len(raw))
	for _, item := range raw {
		rows = append(rows, RunRow{
			RunID:    stringValue(item["run_id"]),
			TargetID: stringValue(item["target_id"]),
			Profile:  stringValue(item["profile"]),
			State:    stringValue(item["state"]),
			Overall:  stringValue(item["overall"]),
		})
	}
	return rows, nil
}

// pythonBin shares deploy's interpreter resolution order:
// ARBITER_ENGINE_PYTHON, then PYTHON, then python3, via exec.LookPath.
func pythonBin() string {
	return deploy.ResolvePython("")
}

func stringValue(value any) string {
	if value == nil {
		return ""
	}
	text, _ := value.(string)
	return text
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
