package engineclient

import (
	"context"
	"encoding/json"
	stderrors "errors"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
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
			replayTranscript(t, repo, path)
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
