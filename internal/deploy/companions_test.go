package deploy

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
)

// ADR-0010/0011/0012 接线测试:伙伴诊断服务器、引擎解析阶梯、起手棋谱规约。

func TestInitWiresCompanionsInstalledMode(t *testing.T) {
	root := t.TempDir()
	if _, err := InitWithOptions(root, testInitOptions()); err != nil {
		t.Fatal(err)
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
		if entry["type"] != "stdio" || entry["command"] != "/test/python" {
			t.Fatalf("%s entry = %#v", name, entry)
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

	agent := readText(t, filepath.Join(root, fileDebugger))
	for _, want := range []string{
		"args: [serve, executor]",
		"mcp__arbiter-executor__SubmitTask",
		"mcp__gdb-mcp__gdb_snapshot",
		"mcp__perf-mcp__perf.scan_c",
		"command: /test/python",
	} {
		if !strings.Contains(agent, want) {
			t.Fatalf("debugger agent missing %q", want)
		}
	}
	info, err := os.Stat(filepath.Join(root, fileDebugger))
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("debugger agent mode/err = %v %v", info.Mode().Perm(), err)
	}
}

func TestInitLadderFallsBackToEmbeddedAndWiresPythonPath(t *testing.T) {
	root := t.TempDir()
	opts := testInitOptions()
	calls := 0
	opts.VerifyEngine = func(python, repo string) (string, error) {
		calls++
		if calls == 1 {
			return "", errors.New("no installed package") // 安装包探测失败 → 自动回退
		}
		return "embedded-engine", nil
	}
	if _, err := InitWithOptions(root, opts); err != nil {
		t.Fatal(err)
	}
	if calls != 2 {
		t.Fatalf("verify calls = %d, want 2 (installed probe + embedded verify)", calls)
	}
	if _, err := os.Stat(filepath.Join(root, ".arbiter", "engine", "arbiter_engine", "gdbmcp", "cli.py")); err != nil {
		t.Fatalf("embedded engine missing companions: %v", err)
	}
	var mcpRoot map[string]any
	readJSONFile(t, filepath.Join(root, fileMCP), &mcpRoot)
	servers := mcpRoot["mcpServers"].(map[string]any)
	for _, name := range []string{"gdb-mcp", "perf-mcp"} {
		entry := servers[name].(map[string]any)
		env, ok := entry["env"].(map[string]any)
		if !ok || env["PYTHONPATH"] != ".arbiter/engine" {
			t.Fatalf("%s env = %#v, want PYTHONPATH=.arbiter/engine", name, entry["env"])
		}
	}
	agent := readText(t, filepath.Join(root, fileDebugger))
	if !strings.Contains(agent, "PYTHONPATH: .arbiter/engine") {
		t.Fatal("debugger agent missing embedded PYTHONPATH")
	}
}

func TestInitPreservesForeignCompanionEntries(t *testing.T) {
	root := t.TempDir()
	writeJSONFile(t, filepath.Join(root, fileMCP), map[string]any{
		"mcpServers": map[string]any{
			"gdb-mcp": map[string]any{"type": "stdio", "command": "/opt/custom/python", "args": []any{"-m", "gdb_mcp", "serve"}},
		},
	})
	if _, err := InitWithOptions(root, testInitOptions()); err != nil {
		t.Fatal(err)
	}
	var mcpRoot map[string]any
	readJSONFile(t, filepath.Join(root, fileMCP), &mcpRoot)
	servers := mcpRoot["mcpServers"].(map[string]any)
	if servers["gdb-mcp"].(map[string]any)["command"] != "/opt/custom/python" {
		t.Fatalf("foreign gdb-mcp entry clobbered: %#v", servers["gdb-mcp"])
	}
	if _, ok := servers["perf-mcp"]; !ok {
		t.Fatalf("perf-mcp not added alongside preserved entry: %#v", servers)
	}
}

func TestStarterOpeningsFollowConventionAndSurviveEdits(t *testing.T) {
	// ADR-0012 命名规约 lint:仅约束 starter intent 集(templates/openings/);
	// 设计钦定的 intro 系棋谱(freeplay/gold-digger/…)不在此列。
	entries, err := templates.ReadDir("templates/openings")
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) == 0 {
		t.Fatal("no starter openings")
	}
	for _, entry := range entries {
		data := mustTemplate("templates/openings/" + entry.Name())
		book, issues := playbook.ParseBytes(entry.Name(), []byte(data))
		if len(issues) > 0 {
			t.Errorf("%s: %#v", entry.Name(), issues)
			continue
		}
		stem := strings.TrimSuffix(entry.Name(), ".md")
		if book.Name != stem {
			t.Errorf("%s: name %q != file stem", entry.Name(), book.Name)
		}
		if parts := strings.Split(book.Name, "-"); len(parts) > 3 {
			t.Errorf("%s: name has %d segments, convention allows <=3", entry.Name(), len(parts))
		}
		if !strings.HasPrefix(book.Description, "Use when") {
			t.Errorf("%s: description must lead with 'Use when'", entry.Name())
		}
		if !strings.Contains(book.Description, "Do not use") {
			t.Errorf("%s: description must carry a 'Do not use … (use <other>)' cross-pointer", entry.Name())
		}
	}

	// write-if-missing:用户改过的棋谱第二次 init 绝不覆盖。
	root := t.TempDir()
	if _, err := InitWithOptions(root, testInitOptions()); err != nil {
		t.Fatal(err)
	}
	edited := filepath.Join(root, dirPlaybook, "build-feature.md")
	if err := os.WriteFile(edited, []byte("user owns this\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := InitWithOptions(root, testInitOptions()); err != nil {
		t.Fatal(err)
	}
	if got := readText(t, edited); got != "user owns this\n" {
		t.Fatalf("user-edited opening overwritten: %q", got)
	}
}
