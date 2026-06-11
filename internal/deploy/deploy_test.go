package deploy

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

func TestInitMergesAndIsIdempotent(t *testing.T) {
	root := t.TempDir()
	writeJSONFile(t, filepath.Join(root, fileMCP), map[string]any{
		"keep": "yes",
		"mcpServers": map[string]any{
			"other": map[string]any{"type": "stdio", "command": "other"},
		},
	})
	writeJSONFile(t, filepath.Join(root, fileSettings), map[string]any{
		"existing": "value",
		"permissions": map[string]any{
			"deny": []any{"Read(existing/**)"},
		},
	})

	first, err := Init(root)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(first, "arbiter 已部署") {
		t.Fatalf("guidance = %q", first)
	}
	before := snapshot(t, root)
	second, err := Init(root)
	if err != nil {
		t.Fatal(err)
	}
	if second == "" {
		t.Fatal("empty guidance")
	}
	after := snapshot(t, root)
	if len(before) != len(after) {
		t.Fatalf("snapshot size changed: %d -> %d", len(before), len(after))
	}
	for path, data := range before {
		if string(after[path]) != string(data) {
			t.Fatalf("file changed on second init: %s", path)
		}
	}

	var mcpRoot map[string]any
	readJSONFile(t, filepath.Join(root, fileMCP), &mcpRoot)
	if mcpRoot["keep"] != "yes" {
		t.Fatalf("mcp lost field: %#v", mcpRoot)
	}
	servers := mcpRoot["mcpServers"].(map[string]any)
	if _, ok := servers["other"]; !ok {
		t.Fatalf("mcp lost server: %#v", servers)
	}

	var settings map[string]any
	readJSONFile(t, filepath.Join(root, fileSettings), &settings)
	if settings["existing"] != "value" {
		t.Fatalf("settings lost field: %#v", settings)
	}
	key1, err := os.ReadFile(filepath.Join(root, fileSeatKey))
	if err != nil {
		t.Fatal(err)
	}
	if len(strings.TrimSpace(string(key1))) != 32 {
		t.Fatalf("seat key length = %d", len(strings.TrimSpace(string(key1))))
	}
}

func TestSeatKeyRegeneratedWhenMissing(t *testing.T) {
	root := t.TempDir()
	if _, err := Init(root); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, fileSeatKey)
	first, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if _, err := Init(root); err != nil {
		t.Fatal(err)
	}
	second, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(first) == string(second) {
		t.Fatal("seat key was not regenerated")
	}
}

func TestInitReportsMCPReplacement(t *testing.T) {
	root := t.TempDir()
	writeJSONFile(t, filepath.Join(root, fileMCP), map[string]any{
		"mcpServers": map[string]any{
			"arbiter": map[string]any{"type": "stdio", "command": "/tmp/old-arbiter"},
		},
	})
	msg, err := Init(root)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(msg, "既有 arbiter 服务器指向不同命令") {
		t.Fatalf("missing replacement hint: %q", msg)
	}
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

func readJSONFile(t *testing.T, path string, out any) {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, out); err != nil {
		t.Fatal(err)
	}
}

func snapshot(t *testing.T, root string) map[string][]byte {
	t.Helper()
	out := map[string][]byte{}
	var paths []string
	if err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		paths = append(paths, path)
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	sort.Strings(paths)
	for _, path := range paths {
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			t.Fatal(err)
		}
		out[rel] = data
	}
	return out
}
