package deploy

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

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
		"gdb-mcp":  {"-m", "arbiter_engine.gdbmcp", "serve", "--root", root},
		"perf-mcp": {"-m", "arbiter_engine.perfmcp", "serve", "--root", root},
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
		"args: [serve, executor, --root, " + root + "]",
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
	wantPythonPath := filepath.Join(root, ".arbiter", "engine")
	for _, name := range []string{"gdb-mcp", "perf-mcp"} {
		entry := servers[name].(map[string]any)
		env, ok := entry["env"].(map[string]any)
		if !ok || env["PYTHONPATH"] != wantPythonPath {
			t.Fatalf("%s env = %#v, want absolute PYTHONPATH %q", name, entry["env"], wantPythonPath)
		}
	}
	agent := readText(t, filepath.Join(root, fileDebugger))
	if !strings.Contains(agent, "PYTHONPATH: "+wantPythonPath) {
		t.Fatal("debugger agent missing absolute embedded PYTHONPATH")
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

// 旧版 init 写出的相对路径条目(-m arbiter_engine.*)是本工具的产物:
// 重跑 init 必须刷新成绝对路径形态 —— 这是用户 -32000 故障的自愈路径。
func TestInitRefreshesStaleEngineCompanionEntries(t *testing.T) {
	root := t.TempDir()
	writeJSONFile(t, filepath.Join(root, fileMCP), map[string]any{
		"mcpServers": map[string]any{
			"gdb-mcp": map[string]any{
				"type": "stdio", "command": "/usr/bin/python3",
				"args": []any{"-m", "arbiter_engine.gdbmcp", "serve", "--root", "."},
				"env":  map[string]any{"PYTHONPATH": ".arbiter/engine"},
			},
		},
	})
	if _, err := InitWithOptions(root, testInitOptions()); err != nil {
		t.Fatal(err)
	}
	var mcpRoot map[string]any
	readJSONFile(t, filepath.Join(root, fileMCP), &mcpRoot)
	entry := mcpRoot["mcpServers"].(map[string]any)["gdb-mcp"].(map[string]any)
	args := entry["args"].([]any)
	rootArg := args[len(args)-1].(string)
	if !filepath.IsAbs(rootArg) {
		t.Fatalf("stale entry not refreshed to absolute --root: %#v", args)
	}
	if entry["command"] != "/test/python" {
		t.Fatalf("stale entry command not refreshed: %#v", entry)
	}
}

// 旧 pip 安装包(版本与二进制不符)必须被阶梯拒绝并回退内置引擎 ——
// 这正是用户实测里 perfmcp 不认 --root 的根因(installed 模式选中了
// 过旧的 arbiter-engine)。
func TestInitRejectsStaleInstalledEngine(t *testing.T) {
	root := t.TempDir()
	opts := testInitOptions()
	opts.VerifyEngine = func(python, repo string) (string, error) {
		return "0.0.1", nil // 安装包可导入,但版本过旧
	}
	msg, err := InitWithOptions(root, opts)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(msg, "不匹配") {
		t.Fatalf("guidance silent about stale installed engine: %q", msg)
	}
	// 回退到 embedded:引擎树就位,条目带绝对 PYTHONPATH。
	if _, err := os.Stat(filepath.Join(root, ".arbiter", "engine", "arbiter_engine", "perfmcp", "cli.py")); err != nil {
		t.Fatalf("embedded engine not materialized: %v", err)
	}
	var mcpRoot map[string]any
	readJSONFile(t, filepath.Join(root, fileMCP), &mcpRoot)
	entry := mcpRoot["mcpServers"].(map[string]any)["perf-mcp"].(map[string]any)
	env, ok := entry["env"].(map[string]any)
	if !ok || env["PYTHONPATH"] != filepath.Join(root, ".arbiter", "engine") {
		t.Fatalf("perf-mcp env = %#v", entry["env"])
	}
}

// TestInitVerifiesCompanionHandshakesForReal 是用户实测回归:Linux 裸机
// make install → init → Claude 里 gdb-mcp/perf-mcp reconnect -32000。
// 根因是相对 PYTHONPATH/--root 依赖宿主 cwd;现在条目全绝对路径,且 init
// 以 Claude 同款方式真实拉起两个服务器完成 initialize 握手。
// 注意 cwd 故意设为 "/":握手必须不依赖工作目录。
func TestInitVerifiesCompanionHandshakesForReal(t *testing.T) {
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("python3 not available")
	}
	root := t.TempDir()
	opts := Options{
		FSKind: "apfs",
		Now:    func() time.Time { return time.Date(2026, 6, 12, 0, 0, 0, 0, time.UTC) },
	}
	if _, err := InitWithOptions(root, opts); err != nil {
		t.Fatalf("real init failed: %v", err)
	}
	var mcpRoot map[string]any
	readJSONFile(t, filepath.Join(root, fileMCP), &mcpRoot)
	servers := mcpRoot["mcpServers"].(map[string]any)
	for _, name := range []string{"gdb-mcp", "perf-mcp"} {
		entry := servers[name].(map[string]any)
		env, _ := entry["env"].(map[string]any)
		pythonPath, _ := env["PYTHONPATH"].(string)
		if !filepath.IsAbs(pythonPath) {
			t.Fatalf("%s PYTHONPATH must be absolute, got %q", name, pythonPath)
		}
		args := entry["args"].([]any)
		rootArg := args[len(args)-1].(string)
		if !filepath.IsAbs(rootArg) {
			t.Fatalf("%s --root must be absolute, got %q", name, rootArg)
		}
		// Claude 同款拉起,但 cwd= "/":绝对路径条目必须照常握手。
		comp := companion{Name: name, Command: entry["command"].(string), PythonPath: pythonPath}
		for _, a := range args {
			comp.Args = append(comp.Args, a.(string))
		}
		if err := verifyCompanion("/", comp); err != nil {
			t.Fatalf("%s handshake with cwd=/ failed: %v", name, err)
		}
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
