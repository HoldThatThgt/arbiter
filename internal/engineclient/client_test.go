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

	"github.com/HoldThatThgt/arbiter/internal/embeddedengine"
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

func TestSpawnEmbeddedEngineVerifiesDigestAndJournals(t *testing.T) {
	repo := t.TempDir()
	manifest, err := embeddedengine.Unpack(repo)
	if err != nil {
		t.Fatal(err)
	}
	writeJSONFile(t, filepath.Join(repo, ".arbiter", "run", "engines.json"), map[string]any{
		"mode":          "embedded",
		"engine_root":   ".arbiter/engine",
		"engine_digest": manifest.Digest,
	})
	engine, err := Spawn(context.Background(), RoleQuery, repo)
	if err != nil {
		t.Fatal(err)
	}
	engine.Close()
	data, err := os.ReadFile(filepath.Join(repo, ".arbiter", "match", "log", "journal.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(data), "embedded_engine_verified") {
		t.Fatalf("journal = %s", data)
	}

	// Verification results are memoized per (root, digest) for the process
	// lifetime, so tampering must be exercised in a fresh repo that has not
	// yet verified successfully.
	tampered := t.TempDir()
	tamperedManifest, err := embeddedengine.Unpack(tampered)
	if err != nil {
		t.Fatal(err)
	}
	writeJSONFile(t, filepath.Join(tampered, ".arbiter", "run", "engines.json"), map[string]any{
		"mode":          "embedded",
		"engine_root":   ".arbiter/engine",
		"engine_digest": tamperedManifest.Digest,
	})
	if err := os.WriteFile(filepath.Join(tampered, ".arbiter", "engine", "arbiter_engine", "__init__.py"), []byte("tampered = True\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := Spawn(context.Background(), RoleQuery, tampered); err == nil || !strings.Contains(err.Error(), "embedded engine digest mismatch") {
		t.Fatalf("err = %v, want digest mismatch", err)
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

func TestStartRunAndRunStatusCallCustomMethods(t *testing.T) {
	repo := repoRoot(t)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	client, err := Spawn(ctx, RoleExec, repo)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()

	started, err := client.StartRun(ctx, map[string]any{
		"kind":      "stub",
		"sleep_ms":  10,
		"timeout_s": 1,
		"result": map[string]any{
			"overall": "passed",
		},
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if started.RunID == "" {
		t.Fatal("startRun returned empty run_id")
	}
	if started.State != "running" {
		t.Fatalf("startRun state = %q, want running", started.State)
	}

	var status RunStatus
	for deadline := time.Now().Add(2 * time.Second); time.Now().Before(deadline); {
		status, err = client.RunStatus(ctx, started.RunID)
		if err != nil {
			t.Fatal(err)
		}
		if status.State != "running" {
			break
		}
		time.Sleep(25 * time.Millisecond)
	}
	if status.State != "completed" {
		t.Fatalf("runStatus state = %q, want completed", status.State)
	}
	if !strings.Contains(string(status.Result), `"overall":"passed"`) {
		t.Fatalf("runStatus result = %s", status.Result)
	}
}

func TestToolsListAndCallToolWithMeta(t *testing.T) {
	repo := repoRoot(t)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
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
	if len(tools) == 0 || tools[0].Name == "" {
		t.Fatalf("tools = %#v", tools)
	}

	result, err := client.CallTool(ctx, "search", map[string]any{"query": "callers:main"}, map[string]any{"match_id": "m1"})
	if err != nil {
		t.Fatal(err)
	}
	if result.IsError {
		t.Fatalf("CallTool returned error result: %#v", result)
	}
	if result.StructuredContent["query"] != "callers:main" || result.StructuredContent["query_kind"] != "relation" {
		t.Fatalf("result = %#v", result)
	}
}

func TestTimeoutPoisonsKillsProcessGroupAndRespawnWorks(t *testing.T) {
	dir := t.TempDir()
	marker := filepath.Join(dir, "respond")
	childFile := filepath.Join(dir, "child.pid")
	script := writeFakeEngine(t, `
import json, os, subprocess, sys, time
marker = sys.argv[1]
child_file = sys.argv[2]
if not os.path.exists(marker):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    open(child_file, "w", encoding="utf-8").write(str(child.pid))
    sys.stdout.flush()
    for _line in sys.stdin:
        time.sleep(30)
else:
    for line in sys.stdin:
        req = json.loads(line)
        sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":req["id"],"result":{"ok":True}}, separators=(",", ":")) + "\n")
        sys.stdout.flush()
`)

	ctx := context.Background()
	client, err := spawnCommand(ctx, RoleExec, dir, []string{pythonBin(), script, marker, childFile})
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()

	callCtx, cancel := context.WithTimeout(ctx, 100*time.Millisecond)
	_, err = client.Call(callCtx, "probe", nil)
	cancel()
	if err == nil {
		t.Fatal("expected timeout")
	}
	if !client.Poisoned() {
		t.Fatal("client not poisoned after timeout")
	}

	childPID := readPID(t, childFile)
	waitNoProcess(t, childPID)

	if err := os.WriteFile(marker, []byte("ok"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := client.Respawn(ctx); err != nil {
		t.Fatal(err)
	}
	if client.Poisoned() {
		t.Fatal("client still poisoned after respawn")
	}
	raw, err := client.Call(ctx, "probe", nil)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(raw), `"ok":true`) {
		t.Fatalf("response = %s", raw)
	}
}

func TestStdinWriteFailurePoisonsChild(t *testing.T) {
	dir := t.TempDir()
	ready := filepath.Join(dir, "ready")
	script := writeFakeEngine(t, `
import os, sys, time
os.close(0)
open(sys.argv[1], "w", encoding="utf-8").write("ready")
time.sleep(30)
`)

	client, err := spawnCommand(context.Background(), RoleExec, dir, []string{pythonBin(), script, ready})
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	waitForFile(t, ready)
	pid := client.cmd.Process.Pid

	_, err = client.Call(context.Background(), "probe", nil)
	if err == nil {
		t.Fatal("expected stdin write failure")
	}
	if !client.Poisoned() {
		t.Fatal("client not poisoned after stdin write failure")
	}
	waitNoProcess(t, pid)
	if _, err := client.Call(context.Background(), "probe", nil); !stderrors.Is(err, ErrPoisoned) {
		t.Fatalf("err = %v, want ErrPoisoned", err)
	}
}

func TestSpawnSetsArbiterBinEnv(t *testing.T) {
	dir := t.TempDir()
	out := filepath.Join(dir, "env.txt")
	script := writeFakeEngine(t, `
import json, os, sys
open(sys.argv[1], "w", encoding="utf-8").write(os.environ.get("ARBITER_BIN", ""))
for line in sys.stdin:
    req = json.loads(line)
    sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":req["id"],"result":{"ok":True}}, separators=(",", ":")) + "\n")
    sys.stdout.flush()
`)

	client, err := spawnCommand(context.Background(), RoleExec, dir, []string{pythonBin(), script, out})
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	if _, err := client.Call(context.Background(), "probe", nil); err != nil {
		t.Fatal(err)
	}

	data, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	got := strings.TrimSpace(string(data))
	want, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	if abs, err := filepath.Abs(want); err == nil {
		want = abs
	}
	if got != want {
		t.Fatalf("ARBITER_BIN = %q, want %q", got, want)
	}
	if !filepath.IsAbs(got) {
		t.Fatalf("ARBITER_BIN = %q is not absolute", got)
	}
}

func TestCallTimeoutEnvOverride(t *testing.T) {
	cases := []struct {
		value string
		want  time.Duration
	}{
		{"", defaultCallTimeout},
		{"abc", defaultCallTimeout},
		{"0", defaultCallTimeout},
		{"-3", defaultCallTimeout},
		{"42", 42 * time.Second},
	}
	for _, tc := range cases {
		t.Setenv(callTimeoutEnv, tc.value)
		if got := callTimeout(); got != tc.want {
			t.Fatalf("callTimeout with %s=%q is %v, want %v", callTimeoutEnv, tc.value, got, tc.want)
		}
	}
}

func TestCallTimeoutEnvBoundsCallWithoutParentDeadline(t *testing.T) {
	t.Setenv(callTimeoutEnv, "1")
	dir := t.TempDir()
	script := writeFakeEngine(t, `
import sys, time
sys.stdin.readline()
time.sleep(30)
`)

	client, err := spawnCommand(context.Background(), RoleExec, dir, []string{pythonBin(), script})
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()

	start := time.Now()
	_, err = client.Call(context.Background(), "probe", nil)
	if !stderrors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("err = %v, want deadline exceeded", err)
	}
	if elapsed := time.Since(start); elapsed >= 10*time.Second {
		t.Fatalf("call took %v, want roughly the 1s override", elapsed)
	}
	if !client.Poisoned() {
		t.Fatal("client not poisoned after timeout")
	}
}

func TestProtocolFaultsPoisonChild(t *testing.T) {
	cases := []struct {
		name   string
		source string
	}{
		{
			name: "garbage",
			source: `
import sys
sys.stdin.readline()
sys.stdout.write("not-json\n")
sys.stdout.flush()
`,
		},
		{
			name: "death",
			source: `
import sys
sys.stdin.readline()
sys.exit(0)
`,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			script := writeFakeEngine(t, tc.source)
			client, err := spawnCommand(context.Background(), RoleExec, dir, []string{pythonBin(), script})
			if err != nil {
				t.Fatal(err)
			}
			defer client.Close()

			_, err = client.Call(context.Background(), "probe", nil)
			if err == nil {
				t.Fatal("expected protocol fault")
			}
			if !client.Poisoned() {
				t.Fatal("client not poisoned after protocol fault")
			}
		})
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
		if decoded.Method == "arbiter/startRun" {
			time.Sleep(100 * time.Millisecond)
		}
	}
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

func writeJSONFile(t *testing.T, path string, value any) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, data, 0o644); err != nil {
		t.Fatal(err)
	}
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

func writeFakeEngine(t *testing.T, source string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "fake_engine.py")
	if err := os.WriteFile(path, []byte(strings.TrimLeft(source, "\n")), 0o755); err != nil {
		t.Fatal(err)
	}
	return path
}

func pythonBin() string {
	if python := os.Getenv("PYTHON"); python != "" {
		return python
	}
	return "python3"
}

func waitForFile(t *testing.T, path string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(path); err == nil {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("file was not created: %s", path)
}

func readPID(t *testing.T, path string) int {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		data, err := os.ReadFile(path)
		if err == nil {
			pid, convErr := strconv.Atoi(strings.TrimSpace(string(data)))
			if convErr != nil {
				t.Fatal(convErr)
			}
			return pid
		}
		time.Sleep(25 * time.Millisecond)
	}
	t.Fatalf("pid file was not written: %s", path)
	return 0
}

func waitNoProcess(t *testing.T, pid int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		err := syscall.Kill(pid, 0)
		if err == syscall.ESRCH {
			return
		}
		if err != nil && !stderrors.Is(err, syscall.EPERM) {
			return
		}
		time.Sleep(25 * time.Millisecond)
	}
	t.Fatal(fmt.Sprintf("process %d still exists", pid))
}
