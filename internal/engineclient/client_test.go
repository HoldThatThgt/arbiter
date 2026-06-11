package engineclient

import (
	"context"
	"encoding/json"
	stderrors "errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

type transcriptEntry struct {
	Type          string          `json:"type"`
	Message       json.RawMessage `json:"message"`
	AllowVolatile []string        `json:"allow_volatile"`
}

type transcriptRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      int64           `json:"id"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params"`
}

func TestReplayTranscriptsAgainstPythonStub(t *testing.T) {
	repo := repoRoot(t)
	paths, err := filepath.Glob(filepath.Join(repo, "testdata", "transcripts", "*.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	if len(paths) == 0 {
		t.Fatal("expected at least one transcript")
	}

	for _, path := range paths {
		t.Run(filepath.Base(path), func(t *testing.T) {
			workdir := transcriptWorkdir(t, repo)
			replayTranscript(t, workdir, path)
		})
	}
}

func TestValidateResponseReturnsTypedEngineError(t *testing.T) {
	line := []byte(`{"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"no snapshot","data":{"kind":"no_snapshot","hint":"run the gear-up step"}}}`)

	err := validateResponse(line, 1)

	var engineErr *EngineError
	if !stderrors.As(err, &engineErr) {
		t.Fatalf("error = %T %[1]v, want *EngineError", err)
	}
	if engineErr.Code != -32000 {
		t.Fatalf("code = %d", engineErr.Code)
	}
	if engineErr.Kind != "no_snapshot" {
		t.Fatalf("kind = %q", engineErr.Kind)
	}
	if !strings.Contains(string(engineErr.Data), `"hint":"run the gear-up step"`) {
		t.Fatalf("data = %s", engineErr.Data)
	}
	if string(engineErr.Response) != string(line) {
		t.Fatalf("response = %s", engineErr.Response)
	}
}

func TestValidateResponseRejectsUnknownEngineErrorKind(t *testing.T) {
	line := []byte(`{"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"new","data":{"kind":"new_kind"}}}`)

	err := validateResponse(line, 1)

	var engineErr *EngineError
	if stderrors.As(err, &engineErr) {
		t.Fatalf("unknown kind produced EngineError: %#v", engineErr)
	}
	if err == nil || !strings.Contains(err.Error(), "unknown engine error kind") {
		t.Fatalf("err = %v", err)
	}
}

func TestStartRunAndRunStatusMethods(t *testing.T) {
	repo := repoRoot(t)
	workdir := transcriptWorkdir(t, repo)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	client, err := Spawn(ctx, RoleExec, workdir)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()

	runID, err := client.StartRun(ctx, map[string]any{
		"duration_ms": 0,
		"timeout_ms":  1000,
	})
	if err != nil {
		t.Fatal(err)
	}
	if runID == "" {
		t.Fatal("runID is empty")
	}

	status := waitRunStatus(t, ctx, client, runID)
	if status.Status != "finished" {
		t.Fatalf("status = %q, want finished", status.Status)
	}

	var result struct {
		RunID   string `json:"run_id"`
		Overall string `json:"overall"`
	}
	if err := json.Unmarshal(status.Result, &result); err != nil {
		t.Fatal(err)
	}
	if result.RunID != runID || result.Overall != "passed" {
		t.Fatalf("result = %#v, want run_id %q overall passed", result, runID)
	}
}

func TestToolWrappersSendMetaAndDecodeResults(t *testing.T) {
	repo := fakeEngineRepo(t, `
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    if method == "tools/list":
        result = {"tools": [{"name": "probe", "description": "Probe tool", "inputSchema": {"type": "object", "additionalProperties": False}}]}
    elif method == "tools/call":
        params = request.get("params", {})
        result = {"isError": False, "content": [], "tool": params.get("name"), "seen_meta": params.get("_meta")}
    else:
        result = {"ok": method}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}, separators=(",", ":")), flush=True)
`)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	client, err := Spawn(ctx, RoleQuery, repo)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()

	tools, err := client.ToolsList(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(tools) != 1 || tools[0].Name != "probe" || tools[0].Description != "Probe tool" {
		t.Fatalf("tools = %#v", tools)
	}

	result, err := client.CallTool(ctx, "probe", map[string]any{}, map[string]any{"match_id": "m1"})
	if err != nil {
		t.Fatal(err)
	}
	if result.Tool != "probe" || result.IsError {
		t.Fatalf("result = %#v", result)
	}
	if !strings.Contains(string(result.Raw), `"match_id":"m1"`) {
		t.Fatalf("raw result missing meta echo: %s", result.Raw)
	}
}

func TestCustomMethodWrappers(t *testing.T) {
	repo := fakeEngineRepo(t, `
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"method": request["method"], "params": request.get("params")}}, separators=(",", ":")), flush=True)
`)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	client, err := Spawn(ctx, RoleExec, repo)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()

	calls := []struct {
		name string
		call func() (json.RawMessage, error)
		want string
	}{
		{"Refresh", func() (json.RawMessage, error) { return client.Refresh(ctx, map[string]any{"scope": "worktree"}, nil) }, "arbiter/refresh"},
		{"Census", func() (json.RawMessage, error) { return client.Census(ctx, map[string]any{"scope": "src"}, nil) }, "arbiter/census"},
		{"ResolveBriefing", func() (json.RawMessage, error) { return client.ResolveBriefing(ctx, []string{"code:function:1"}, nil) }, "arbiter/resolveBriefing"},
	}
	for _, tc := range calls {
		t.Run(tc.name, func(t *testing.T) {
			raw, err := tc.call()
			if err != nil {
				t.Fatal(err)
			}
			if !strings.Contains(string(raw), tc.want) {
				t.Fatalf("raw = %s, want method %s", raw, tc.want)
			}
		})
	}
}

func TestTimeoutPoisonsChildAndKillsProcessGroup(t *testing.T) {
	pidFile := filepath.Join(t.TempDir(), "child.pid")
	repo := fakeEngineRepo(t, fmt.Sprintf(`
import json
import subprocess
import sys
import time

for line in sys.stdin:
    request = json.loads(line)
    if request["method"] == "hang":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        with open(%q, "w", encoding="utf-8") as handle:
            handle.write(str(child.pid))
        time.sleep(30)
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"ok": True}}, separators=(",", ":")), flush=True)
`, pidFile))
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	client, err := Spawn(ctx, RoleExec, repo)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()

	callCtx, cancelCall := context.WithTimeout(ctx, 250*time.Millisecond)
	defer cancelCall()
	_, err = client.Call(callCtx, "hang", nil)
	if !stderrors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("err = %v, want deadline exceeded", err)
	}
	childPID := readPIDFile(t, pidFile)
	eventuallyProcessGone(t, childPID)

	_, err = client.Call(ctx, "initialize", nil)
	if !stderrors.Is(err, ErrPoisoned) {
		t.Fatalf("reuse err = %v, want ErrPoisoned", err)
	}
}

func TestFaultInjectionPoisonsOnGarbageLineAndDeath(t *testing.T) {
	tests := []struct {
		name   string
		source string
	}{
		{
			name: "garbage",
			source: `
import sys

sys.stdout.write("not json\n")
sys.stdout.flush()
time.sleep(30)
`,
		},
		{
			name: "death",
			source: `
import sys

sys.exit(7)
`,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			repo := fakeEngineRepo(t, tc.source)
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			client, err := Spawn(ctx, RoleExec, repo)
			if err != nil {
				t.Fatal(err)
			}
			defer client.Close()

			_, err = client.Call(ctx, "initialize", nil)
			if err == nil {
				t.Fatal("expected call error")
			}
			_, err = client.Call(ctx, "initialize", nil)
			if !stderrors.Is(err, ErrPoisoned) {
				t.Fatalf("reuse err = %v, want ErrPoisoned", err)
			}
		})
	}
}

func TestSpawnCreatesProcessGroupAndCloseUsesEOF(t *testing.T) {
	repo := fakeEngineRepo(t, `
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"ok": True}}, separators=(",", ":")), flush=True)
`)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	client, err := Spawn(ctx, RoleQuery, repo)
	if err != nil {
		t.Fatal(err)
	}

	pgid, err := syscall.Getpgid(client.cmd.Process.Pid)
	if err != nil {
		t.Fatal(err)
	}
	if pgid != client.cmd.Process.Pid {
		t.Fatalf("pgid = %d, want child pid %d", pgid, client.cmd.Process.Pid)
	}
	if err := client.Close(); err != nil {
		t.Fatal(err)
	}
}

func replayTranscript(t *testing.T, repo, path string) {
	t.Helper()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	client, err := Spawn(ctx, RoleQuery, repo)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()

	entries := readTranscript(t, path)
	for i := 0; i < len(entries); i += 2 {
		request := entries[i]
		response := entries[i+1]

		var decoded transcriptRequest
		if err := json.Unmarshal(request.Message, &decoded); err != nil {
			t.Fatal(err)
		}
		if decoded.JSONRPC != "2.0" {
			t.Fatalf("request jsonrpc = %q, want 2.0", decoded.JSONRPC)
		}
		if decoded.ID != int64(i/2+1) {
			t.Fatalf("request id = %d, want sequential id %d", decoded.ID, i/2+1)
		}
		if decoded.Method == "arbiter/runStatus" {
			time.Sleep(50 * time.Millisecond)
		}
		actual, err := client.Call(ctx, decoded.Method, decoded.Params)
		if transcriptHasError(t, response.Message) {
			var engineErr *EngineError
			if !stderrors.As(err, &engineErr) {
				t.Fatalf("error = %T %[1]v, want *EngineError", err)
			}
			assertJSONEqual(t, response.Message, engineErr.Response, response.AllowVolatile)
			continue
		}
		if err != nil {
			t.Fatal(err)
		}
		assertJSONEqual(t, response.Message, actual, response.AllowVolatile)
	}
}

func waitRunStatus(t *testing.T, ctx context.Context, client *Engine, runID string) AsyncRunStatus {
	t.Helper()

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		status, err := client.RunStatus(ctx, runID)
		if err != nil {
			t.Fatal(err)
		}
		if status.Status != "running" {
			return status
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("run %s did not finish", runID)
	return AsyncRunStatus{}
}

func transcriptHasError(t *testing.T, message json.RawMessage) bool {
	t.Helper()

	var envelope struct {
		Error *json.RawMessage `json:"error"`
	}
	if err := json.Unmarshal(message, &envelope); err != nil {
		t.Fatal(err)
	}
	return envelope.Error != nil
}

func readTranscript(t *testing.T, path string) []transcriptEntry {
	t.Helper()

	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	lines := bytesLines(data)
	if len(lines)%2 != 0 {
		t.Fatalf("transcript must contain request/response pairs: %s", path)
	}

	entries := make([]transcriptEntry, 0, len(lines))
	for i, line := range lines {
		var entry transcriptEntry
		if err := json.Unmarshal(line, &entry); err != nil {
			t.Fatalf("%s:%d: %v", path, i+1, err)
		}
		if i%2 == 0 && entry.Type != "request" {
			t.Fatalf("%s:%d: got %q, want request", path, i+1, entry.Type)
		}
		if i%2 == 1 && entry.Type != "response" {
			t.Fatalf("%s:%d: got %q, want response", path, i+1, entry.Type)
		}
		entries = append(entries, entry)
	}
	return entries
}

func assertJSONEqual(t *testing.T, expected, actual json.RawMessage, allowVolatile []string) {
	t.Helper()

	var want any
	if err := json.Unmarshal(expected, &want); err != nil {
		t.Fatal(err)
	}
	var got any
	if err := json.Unmarshal(actual, &got); err != nil {
		t.Fatal(err)
	}
	for _, path := range allowVolatile {
		dropJSONPath(t, &want, path)
		dropJSONPath(t, &got, path)
	}
	if !jsonValuesEqual(want, got) {
		wantJSON, _ := json.Marshal(want)
		gotJSON, _ := json.Marshal(got)
		t.Fatalf("response mismatch:\nexpected=%s\nactual=%s", wantJSON, gotJSON)
	}
}

func repoRoot(t *testing.T) string {
	t.Helper()

	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("runtime.Caller failed")
	}
	return filepath.Clean(filepath.Join(filepath.Dir(file), "..", ".."))
}

func transcriptWorkdir(t *testing.T, repo string) string {
	t.Helper()

	workdir := t.TempDir()
	if err := os.Symlink(filepath.Join(repo, "engine"), filepath.Join(workdir, "engine")); err != nil {
		t.Fatalf("link engine into transcript workdir: %v", err)
	}
	return workdir
}

func fakeEngineRepo(t *testing.T, rpcSource string) string {
	t.Helper()

	root := t.TempDir()
	packageDir := filepath.Join(root, "engine", "arbiter_engine")
	if err := os.MkdirAll(packageDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(packageDir, "__init__.py"), []byte(`__version__ = "fake"`+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(packageDir, "rpc.py"), []byte(rpcSource), 0o644); err != nil {
		t.Fatal(err)
	}
	return root
}

func readPIDFile(t *testing.T, path string) int {
	t.Helper()

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		data, err := os.ReadFile(path)
		if err == nil {
			pid, err := strconv.Atoi(strings.TrimSpace(string(data)))
			if err != nil {
				t.Fatal(err)
			}
			return pid
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("pid file %s was not written", path)
	return 0
}

func eventuallyProcessGone(t *testing.T, pid int) {
	t.Helper()

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if err := syscall.Kill(pid, 0); err != nil {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("process %d still exists", pid)
}

func bytesLines(data []byte) [][]byte {
	var lines [][]byte
	for _, line := range strings.Split(string(data), "\n") {
		if len(line) != 0 {
			lines = append(lines, []byte(line))
		}
	}
	return lines
}

func dropJSONPath(t *testing.T, value *any, path string) {
	t.Helper()

	parts := strings.Split(path, ".")
	var cursor any = *value
	for _, part := range parts[:len(parts)-1] {
		switch node := cursor.(type) {
		case map[string]any:
			cursor = node[part]
		case []any:
			index, err := strconv.Atoi(part)
			if err != nil {
				t.Fatalf("invalid volatile path %q: %v", path, err)
			}
			cursor = node[index]
		default:
			t.Fatalf("invalid volatile path %q", path)
		}
	}

	last := parts[len(parts)-1]
	switch node := cursor.(type) {
	case map[string]any:
		delete(node, last)
	case []any:
		index, err := strconv.Atoi(last)
		if err != nil {
			t.Fatalf("invalid volatile path %q: %v", path, err)
		}
		node[index] = nil
	default:
		t.Fatalf("invalid volatile path %q", path)
	}
}

func jsonValuesEqual(left, right any) bool {
	leftJSON, _ := json.Marshal(left)
	rightJSON, _ := json.Marshal(right)
	return string(leftJSON) == string(rightJSON)
}
