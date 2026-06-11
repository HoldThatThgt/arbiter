package deploy

import (
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"
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

	first, err := InitWithOptions(root, testInitOptions())
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(first, "arbiter 已部署") {
		t.Fatalf("guidance = %q", first)
	}
	before := snapshot(t, root)
	second, err := InitWithOptions(root, testInitOptions())
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
	if _, err := InitWithOptions(root, testInitOptions()); err != nil {
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
	if _, err := InitWithOptions(root, testInitOptions()); err != nil {
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
	msg, err := InitWithOptions(root, testInitOptions())
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(msg, "既有 arbiter 服务器指向不同命令") {
		t.Fatalf("missing replacement hint: %q", msg)
	}
}

func TestCuratorAgentCanListTasks(t *testing.T) {
	root := t.TempDir()
	if _, err := InitWithOptions(root, testInitOptions()); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(filepath.Join(root, fileCurator))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(data), "mcp__arbiter-curator__ListTask") {
		t.Fatalf("curator agent is missing ListTask: %s", data)
	}
}

func TestInitWritesUnifiedDeploymentTree(t *testing.T) {
	root := t.TempDir()
	msg, err := InitWithOptions(root, testInitOptions())
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(msg, "arbiter 已部署") {
		t.Fatalf("guidance = %q", msg)
	}

	var engines map[string]any
	readJSONFile(t, filepath.Join(root, ".arbiter", "run", "engines.json"), &engines)
	if engines["python"] != "/test/python" || engines["engine_version"] != "test-engine" {
		t.Fatalf("engines.json = %#v", engines)
	}
	if engines["verified_at"] != "2026-06-11T00:00:00Z" {
		t.Fatalf("verified_at = %#v", engines["verified_at"])
	}

	key := strings.TrimSpace(readText(t, filepath.Join(root, ".arbiter", "match", "seat.key")))
	if len(key) != 32 {
		t.Fatalf("seat key length = %d", len(key))
	}
	assertMode(t, filepath.Join(root, ".arbiter", "match", "seat.key"), 0o600)
	if _, err := os.Stat(filepath.Join(root, ".arbiter", "match", "run", "seat.key")); !os.IsNotExist(err) {
		t.Fatalf("legacy run/seat.key exists or stat failed: %v", err)
	}

	for _, path := range []string{
		".claude/agents/arbiter-curator.md",
		".claude/agents/arbiter-executor.md",
	} {
		data := readText(t, filepath.Join(root, path))
		if !strings.Contains(data, key) {
			t.Fatalf("%s missing seat key", path)
		}
		assertMode(t, filepath.Join(root, path), 0o600)
	}
	for _, path := range []string{
		".claude/skills/arbiter-play/SKILL.md",
		".claude/skills/arbiter-intro/SKILL.md",
		".claude/skills/playbook-create/SKILL.md",
		".arbiter/config.yml",
		".arbiter/recipes.yaml",
	} {
		if _, err := os.Stat(filepath.Join(root, path)); err != nil {
			t.Fatalf("missing %s: %v", path, err)
		}
	}

	var settings map[string]any
	readJSONFile(t, filepath.Join(root, fileSettings), &settings)
	deny := settings["permissions"].(map[string]any)["deny"].([]any)
	for _, want := range []string{
		"Read(.arbiter/playbook/**)",
		"Read(.arbiter/match/**)",
		"Read(.claude/agents/arbiter-*.md)",
	} {
		if !hasLineValue(deny, want) {
			t.Fatalf("missing deny %q in %#v", want, deny)
		}
	}
}

func TestInitEmbeddedEngineAddsWriteDenyRules(t *testing.T) {
	root := t.TempDir()
	opts := testInitOptions()
	opts.EmbeddedEngine = true
	if _, err := InitWithOptions(root, opts); err != nil {
		t.Fatal(err)
	}
	var settings map[string]any
	readJSONFile(t, filepath.Join(root, fileSettings), &settings)
	deny := settings["permissions"].(map[string]any)["deny"].([]any)
	for _, want := range []string{
		"Edit(.arbiter/engine/**)",
		"Write(.arbiter/engine/**)",
	} {
		if !hasLineValue(deny, want) {
			t.Fatalf("missing embedded deny %q in %#v", want, deny)
		}
	}
}

func TestInitNoExecutorSkipsExecutorAgent(t *testing.T) {
	root := t.TempDir()
	opts := testInitOptions()
	opts.NoExecutor = true
	if _, err := InitWithOptions(root, opts); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(root, fileExecutor)); !os.IsNotExist(err) {
		t.Fatalf("executor agent exists or stat failed: %v", err)
	}
}

func TestInitRefusesNetworkFilesystem(t *testing.T) {
	root := t.TempDir()
	opts := testInitOptions()
	opts.FSKind = "nfs"
	_, err := InitWithOptions(root, opts)
	var deployErr *Error
	if !errors.As(err, &deployErr) || deployErr.Kind != "network_filesystem" {
		t.Fatalf("err = %#v, want network_filesystem", err)
	}
}

func TestRemoveRoundTripPreservesForeignContent(t *testing.T) {
	root := t.TempDir()
	writeJSONFile(t, filepath.Join(root, fileMCP), map[string]any{
		"mcpServers": map[string]any{
			"foreign": map[string]any{"type": "stdio", "command": "foreign"},
		},
	})
	writeJSONFile(t, filepath.Join(root, fileSettings), map[string]any{
		"permissions": map[string]any{"deny": []any{"Read(foreign/**)"}},
		"hooks": map[string]any{
			"Stop": []any{map[string]any{"hooks": []any{map[string]any{"type": "command", "command": "foreign stop"}}}},
		},
	})
	if _, err := InitWithOptions(root, testInitOptions()); err != nil {
		t.Fatal(err)
	}
	opts := testInitOptions()
	opts.Remove = true
	if _, err := InitWithOptions(root, opts); err != nil {
		t.Fatal(err)
	}

	var mcpRoot map[string]any
	readJSONFile(t, filepath.Join(root, fileMCP), &mcpRoot)
	servers := mcpRoot["mcpServers"].(map[string]any)
	if _, ok := servers["arbiter"]; ok {
		t.Fatalf("arbiter server was not removed: %#v", servers)
	}
	if _, ok := servers["foreign"]; !ok {
		t.Fatalf("foreign server removed: %#v", servers)
	}

	var settings map[string]any
	readJSONFile(t, filepath.Join(root, fileSettings), &settings)
	deny := settings["permissions"].(map[string]any)["deny"].([]any)
	if !hasLineValue(deny, "Read(foreign/**)") || hasLineValue(deny, "Read(.arbiter/match/**)") {
		t.Fatalf("deny rules = %#v", deny)
	}
	if commands := stopCommands(settings); join(commands) != "foreign stop\n" {
		t.Fatalf("stop commands = %#v", commands)
	}
	for _, path := range []string{
		".arbiter/run/engines.json",
		".arbiter/match/seat.key",
		".claude/agents/arbiter-curator.md",
		".claude/agents/arbiter-executor.md",
	} {
		if _, err := os.Stat(filepath.Join(root, path)); !os.IsNotExist(err) {
			t.Fatalf("%s still exists or stat failed: %v", path, err)
		}
	}
}

// TestDefaultRecipesParsesWithEngineParser proves the default recipes file is
// valid for the engine's strict RecipeBook v2 parser, which rejects the
// mapping form `targets: {}` with "targets must be a sequence".
func TestDefaultRecipesParsesWithEngineParser(t *testing.T) {
	python, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("python3 not available")
	}
	script := `
import sys
sys.path.insert(0, sys.argv[1])
from arbiter_engine.runs import recipes
recipes.parse(sys.stdin.read())
`
	cmd := exec.Command(python, "-c", script, filepath.Join("..", "..", "engine"))
	cmd.Stdin = strings.NewReader(defaultRecipes())
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("engine parser rejected defaultRecipes(): %v\n%s", err, out)
	}
}

func TestGitignoreLifecycleNonEmbedded(t *testing.T) {
	root := t.TempDir()
	// ".arbiter/engine/" is the user's own entry here (non-embedded init
	// never writes it); ".arbiter/match/status.json" mimics a line appended
	// by an older arbiter version.
	writeText(t, filepath.Join(root, fileGitignore), "node_modules/\n.arbiter/engine/\n.arbiter/match/status.json\n")
	if _, err := InitWithOptions(root, testInitOptions()); err != nil {
		t.Fatal(err)
	}
	after := readText(t, filepath.Join(root, fileGitignore))
	if strings.Count(after, ".arbiter/match/status.json") != 1 {
		t.Fatalf("init duplicated or dropped pre-existing legacy line:\n%s", after)
	}
	if !strings.Contains(after, ".arbiter/match/\n") {
		t.Fatalf("init did not append generated lines:\n%s", after)
	}

	opts := testInitOptions()
	opts.Remove = true
	if _, err := InitWithOptions(root, opts); err != nil {
		t.Fatal(err)
	}
	got := readText(t, filepath.Join(root, fileGitignore))
	// Generated and legacy lines go; the user's ".arbiter/engine/" stays
	// because the non-embedded deployment never owned it.
	if got != "node_modules/\n.arbiter/engine/\n" {
		t.Fatalf("gitignore after remove = %q", got)
	}
}

func TestGitignoreInitOmitsRedundantStatusLine(t *testing.T) {
	root := t.TempDir()
	if _, err := InitWithOptions(root, testInitOptions()); err != nil {
		t.Fatal(err)
	}
	got := readText(t, filepath.Join(root, fileGitignore))
	if strings.Contains(got, ".arbiter/match/status.json") {
		t.Fatalf("init wrote redundant status.json line (covered by .arbiter/match/):\n%s", got)
	}
}

func TestGitignoreRemoveStripsEmbeddedEngineLine(t *testing.T) {
	root := t.TempDir()
	opts := testInitOptions()
	opts.EmbeddedEngine = true
	if _, err := InitWithOptions(root, opts); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(readText(t, filepath.Join(root, fileGitignore)), ".arbiter/engine/\n") {
		t.Fatal("embedded init did not append .arbiter/engine/")
	}
	opts.Remove = true
	if _, err := InitWithOptions(root, opts); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(readText(t, filepath.Join(root, fileGitignore)), ".arbiter/engine/") {
		t.Fatal("embedded remove kept .arbiter/engine/")
	}
}

func testInitOptions() Options {
	return Options{
		Python: "/test/python",
		Now:    func() time.Time { return time.Date(2026, 6, 11, 0, 0, 0, 0, time.UTC) },
		VerifyEngine: func(string, string) (string, error) {
			return "test-engine", nil
		},
		FSKind: "apfs",
	}
}

func readText(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func assertMode(t *testing.T, path string, want os.FileMode) {
	t.Helper()
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if got := info.Mode().Perm(); got != want {
		t.Fatalf("%s mode = %03o, want %03o", path, got, want)
	}
}

func join(values []string) string {
	sort.Strings(values)
	return strings.Join(values, "\n") + "\n"
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
