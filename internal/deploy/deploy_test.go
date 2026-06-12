package deploy

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
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

func TestCuratorAgentCanListTasks(t *testing.T) {
	root := t.TempDir()
	if _, err := Init(root); err != nil {
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

func TestInitWiresCompanionDiagnostics(t *testing.T) {
	root := t.TempDir()
	bins := t.TempDir()
	pythonPath := writeFakeBin(t, bins, "python3", 0)
	t.Setenv("PATH", bins)

	msg, err := Init(root)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(msg, "arbiter-debugger") {
		t.Fatalf("guidance is silent about the debugger agent: %q", msg)
	}

	var mcpRoot map[string]any
	readJSONFile(t, filepath.Join(root, fileMCP), &mcpRoot)
	servers := mcpRoot["mcpServers"].(map[string]any)
	wantArgs := map[string][]any{
		"gdb-mcp":  {"-m", "arbiter_engine.gdbmcp", "serve", "--root", "."},
		"perf-mcp": {"-m", "arbiter_engine.perfmcp", "serve"},
	}
	for name, args := range wantArgs {
		entry, ok := servers[name].(map[string]any)
		if !ok {
			t.Fatalf("missing %s server: %#v", name, servers)
		}
		if entry["type"] != "stdio" || entry["command"] != pythonPath {
			t.Fatalf("%s entry = %#v, want command %q", name, entry, pythonPath)
		}
		got, ok := entry["args"].([]any)
		if !ok || len(got) != len(args) {
			t.Fatalf("%s args = %#v, want %#v", name, entry["args"], args)
		}
		for i := range args {
			if got[i] != args[i] {
				t.Fatalf("%s args = %#v, want %#v", name, got, args)
			}
		}
		if _, hasEnv := entry["env"]; hasEnv {
			t.Fatalf("installed mode must not set env on %s: %#v", name, entry)
		}
	}
	if _, err := os.Stat(filepath.Join(root, dirEmbeddedEngine)); !os.IsNotExist(err) {
		t.Fatalf("installed mode must not materialize the embedded engine (err=%v)", err)
	}

	agentPath := filepath.Join(root, fileDebugger)
	info, err := os.Stat(agentPath)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("debugger agent mode = %v, want 0600", info.Mode().Perm())
	}
	agent, err := os.ReadFile(agentPath)
	if err != nil {
		t.Fatal(err)
	}
	key, err := os.ReadFile(filepath.Join(root, fileSeatKey))
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{
		strings.TrimSpace(string(key)),
		"args: [serve, executor]",
		"mcp__arbiter-executor__SubmitTask",
		"mcp__gdb-mcp__gdb_snapshot",
		"mcp__perf-mcp__perf.scan_c",
		"command: " + pythonPath,
		"args: [-m, arbiter_engine.gdbmcp, serve, --root, .]",
		"args: [-m, arbiter_engine.perfmcp, serve]",
	} {
		if !strings.Contains(string(agent), want) {
			t.Fatalf("debugger agent missing %q:\n%s", want, agent)
		}
	}

	var settings map[string]any
	readJSONFile(t, filepath.Join(root, fileSettings), &settings)
	deny := settings["permissions"].(map[string]any)["deny"].([]any)
	if !hasLineValue(deny, "Read(.claude/agents/arbiter-debugger.md)") {
		t.Fatalf("deny rules missing debugger agent: %#v", deny)
	}
	gitignore, err := os.ReadFile(filepath.Join(root, fileGitignore))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(gitignore), ".claude/agents/arbiter-debugger.md") {
		t.Fatalf("gitignore missing debugger agent: %s", gitignore)
	}

	before := snapshot(t, root)
	if _, err := Init(root); err != nil {
		t.Fatal(err)
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
}

func TestInitPreservesForeignCompanionEntries(t *testing.T) {
	root := t.TempDir()
	bins := t.TempDir()
	writeFakeBin(t, bins, "python3", 0)
	t.Setenv("PATH", bins)
	foreign := map[string]any{
		"type":    "stdio",
		"command": "/opt/custom/python",
		"args":    []any{"-m", "gdb_mcp", "serve"},
	}
	writeJSONFile(t, filepath.Join(root, fileMCP), map[string]any{
		"mcpServers": map[string]any{"gdb-mcp": foreign},
	})
	if _, err := Init(root); err != nil {
		t.Fatal(err)
	}
	var mcpRoot map[string]any
	readJSONFile(t, filepath.Join(root, fileMCP), &mcpRoot)
	servers := mcpRoot["mcpServers"].(map[string]any)
	entry := servers["gdb-mcp"].(map[string]any)
	if entry["command"] != "/opt/custom/python" {
		t.Fatalf("foreign gdb-mcp entry was clobbered: %#v", entry)
	}
	if _, ok := servers["perf-mcp"]; !ok {
		t.Fatalf("perf-mcp not added alongside preserved entry: %#v", servers)
	}
}

func TestInitEmbeddedEngineFallback(t *testing.T) {
	root := t.TempDir()
	bins := t.TempDir()
	writeFakeBin(t, bins, "python3", 1) // 已安装包探针失败 ⇒ 释放内置引擎
	t.Setenv("PATH", bins)
	msg, err := Init(root)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(msg, ".arbiter/engine") {
		t.Fatalf("guidance silent about embedded engine: %q", msg)
	}

	for _, rel := range []string{
		"arbiter_engine/__init__.py",
		"arbiter_engine/gdbmcp/cli.py",
		"arbiter_engine/perfmcp/analysis.py",
	} {
		if _, err := os.Stat(filepath.Join(root, dirEmbeddedEngine, rel)); err != nil {
			t.Fatalf("embedded engine missing %s: %v", rel, err)
		}
	}
	digest1, err := os.ReadFile(filepath.Join(root, fileEngineDigest))
	if err != nil {
		t.Fatal(err)
	}

	var mcpRoot map[string]any
	readJSONFile(t, filepath.Join(root, fileMCP), &mcpRoot)
	servers := mcpRoot["mcpServers"].(map[string]any)
	for _, name := range []string{"gdb-mcp", "perf-mcp"} {
		entry, ok := servers[name].(map[string]any)
		if !ok {
			t.Fatalf("missing %s server: %#v", name, servers)
		}
		env, ok := entry["env"].(map[string]any)
		if !ok || env["PYTHONPATH"] != dirEmbeddedEngine {
			t.Fatalf("%s entry env = %#v, want PYTHONPATH=%s", name, entry["env"], dirEmbeddedEngine)
		}
	}

	agent, err := os.ReadFile(filepath.Join(root, fileDebugger))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(agent), "PYTHONPATH: "+dirEmbeddedEngine) {
		t.Fatalf("debugger agent missing embedded PYTHONPATH:\n%s", agent)
	}

	var settings map[string]any
	readJSONFile(t, filepath.Join(root, fileSettings), &settings)
	deny := settings["permissions"].(map[string]any)["deny"].([]any)
	for _, rule := range []string{"Edit(.arbiter/engine/**)", "Write(.arbiter/engine/**)"} {
		if !hasLineValue(deny, rule) {
			t.Fatalf("deny rules missing %q: %#v", rule, deny)
		}
	}
	gitignore, err := os.ReadFile(filepath.Join(root, fileGitignore))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(gitignore), ".arbiter/engine/") {
		t.Fatalf("gitignore missing engine dir: %s", gitignore)
	}

	before := snapshot(t, root)
	if _, err := Init(root); err != nil {
		t.Fatal(err)
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
	digest2, err := os.ReadFile(filepath.Join(root, fileEngineDigest))
	if err != nil {
		t.Fatal(err)
	}
	if string(digest1) != string(digest2) {
		t.Fatal("engine digest changed across idempotent reruns")
	}
}

func TestInitWithoutPythonStaysLean(t *testing.T) {
	root := t.TempDir()
	t.Setenv("PATH", t.TempDir()) // 无 python3:唯一的系统前置缺失
	msg, err := Init(root)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(root, fileDebugger)); !os.IsNotExist(err) {
		t.Fatalf("debugger agent written without python (err=%v)", err)
	}
	if _, err := os.Stat(filepath.Join(root, dirEmbeddedEngine)); !os.IsNotExist(err) {
		t.Fatalf("engine materialized without python (err=%v)", err)
	}
	var mcpRoot map[string]any
	readJSONFile(t, filepath.Join(root, fileMCP), &mcpRoot)
	servers := mcpRoot["mcpServers"].(map[string]any)
	for _, name := range []string{"gdb-mcp", "perf-mcp"} {
		if _, ok := servers[name]; ok {
			t.Fatalf("%s wired without python: %#v", name, servers)
		}
	}
	if !strings.Contains(msg, "python3") {
		t.Fatalf("guidance missing python3 hint: %q", msg)
	}
}

func TestEmbeddedOpeningsParseAndFollowConvention(t *testing.T) {
	entries, err := templates.ReadDir("templates/openings")
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) == 0 {
		t.Fatal("no embedded openings")
	}
	for _, entry := range entries {
		data, err := templates.ReadFile("templates/openings/" + entry.Name())
		if err != nil {
			t.Fatal(err)
		}
		book, issues := playbook.ParseBytes(entry.Name(), data)
		if len(issues) > 0 {
			t.Errorf("%s: %#v", entry.Name(), issues)
			continue
		}
		stem := strings.TrimSuffix(entry.Name(), ".md")
		if book.Name != stem {
			t.Errorf("%s: playbook name %q must equal the file stem", entry.Name(), book.Name)
		}
		// 命名规约:祈使式用户意图短语,kebab-case,≤3 词。
		if parts := strings.Split(book.Name, "-"); len(parts) > 3 {
			t.Errorf("%s: name has %d segments, convention allows <=3", entry.Name(), len(parts))
		}
		// 描述规约:首句 "Use when ..." + 去重指引 "Do not use ..."。
		if !strings.HasPrefix(book.Description, "Use when") {
			t.Errorf("%s: description must lead with 'Use when': %q", entry.Name(), book.Description)
		}
		if !strings.Contains(book.Description, "Do not use") {
			t.Errorf("%s: description must carry a 'Do not use ... (use <other>)' cross-pointer", entry.Name())
		}
	}
}

func TestInitDeliversOpeningsWriteIfMissing(t *testing.T) {
	root := t.TempDir()
	t.Setenv("PATH", t.TempDir())
	msg, err := Init(root)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(msg, "起手棋谱已就位") {
		t.Fatalf("guidance silent about openings: %q", msg)
	}
	names := []string{"fix-reported-bug", "hunt-latent-bugs", "build-feature", "fix-slow-path"}
	for _, name := range names {
		path := filepath.Join(root, dirPlaybook, name+".md")
		book, issues := playbook.ParseFile(path)
		if len(issues) > 0 {
			t.Fatalf("%s: %#v", name, issues)
		}
		if book.Name != name {
			t.Fatalf("%s: parsed name %q", name, book.Name)
		}
	}
	// 用户内容神圣:改过的文件第二次 init 绝不覆盖。
	edited := filepath.Join(root, dirPlaybook, "build-feature.md")
	if err := os.WriteFile(edited, []byte("user owns this\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	msg, err = Init(root)
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(edited)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != "user owns this\n" {
		t.Fatalf("user-edited opening was overwritten: %q", data)
	}
	if !strings.Contains(msg, "未做改动") {
		t.Fatalf("second init guidance should report openings untouched: %q", msg)
	}
}

func writeFakeBin(t *testing.T, dir, name string, exitCode int) string {
	t.Helper()
	path := filepath.Join(dir, name)
	script := "#!/bin/sh\nexit " + strconv.Itoa(exitCode) + "\n"
	if err := os.WriteFile(path, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	resolved, err := filepath.EvalSymlinks(path)
	if err != nil {
		t.Fatal(err)
	}
	return resolved
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
