package verify

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/HoldThatThgt/arbiter/internal/deploy"
	"github.com/HoldThatThgt/arbiter/internal/playbook"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func TestMain(m *testing.M) {
	if os.Getenv("ARBITER_TEST_STUB") == "1" {
		runStub()
		return
	}
	os.Exit(m.Run())
}

func TestShell(t *testing.T) {
	root := t.TempDir()
	pass, err := Execute(context.Background(), root, ResultSpec{Kind: "shell", Command: "printf 'a\\nb\\nc\\n'", OutputLines: 2})
	if err != nil {
		t.Fatal(err)
	}
	if !Pass(pass) || pass.Output != "b\nc" {
		t.Fatalf("pass = %#v", pass)
	}
	fail, err := Execute(context.Background(), root, ResultSpec{Kind: "shell", Command: "exit 3"})
	if err != nil {
		t.Fatal(err)
	}
	if Pass(fail) || fail.ExitCode == nil || *fail.ExitCode != 3 {
		t.Fatalf("fail = %#v", fail)
	}
	timeout, err := Execute(context.Background(), root, ResultSpec{Kind: "shell", Command: "sleep 5", TimeoutS: 1})
	if err != nil {
		t.Fatal(err)
	}
	if timeout.Failure != "timeout" {
		t.Fatalf("timeout = %#v", timeout)
	}
}

func TestMCP(t *testing.T) {
	root := t.TempDir()
	stub := copiedSelf(t)
	writeMCP(t, root, map[string]any{
		"ok": map[string]any{
			"type":    "stdio",
			"command": stub,
			"env":     map[string]any{"ARBITER_TEST_STUB": "1", "ARBITER_TEST_MODE": "ok"},
		},
		"bad": map[string]any{
			"type":    "stdio",
			"command": stub,
			"env":     map[string]any{"ARBITER_TEST_STUB": "1", "ARBITER_TEST_MODE": "bad"},
		},
		"slow": map[string]any{
			"type":    "stdio",
			"command": stub,
			"env":     map[string]any{"ARBITER_TEST_STUB": "1", "ARBITER_TEST_MODE": "slow"},
		},
		"http": map[string]any{
			"type":    "http",
			"command": stub,
		},
	})
	ok, err := Execute(context.Background(), root, ResultSpec{Kind: "mcp", Server: "ok", Tool: "probe"})
	if err != nil {
		t.Fatal(err)
	}
	if !Pass(ok) || ok.IsError == nil || *ok.IsError {
		t.Fatalf("ok = %#v", ok)
	}
	bad, err := Execute(context.Background(), root, ResultSpec{Kind: "mcp", Server: "bad", Tool: "probe"})
	if err != nil {
		t.Fatal(err)
	}
	if Pass(bad) || bad.IsError == nil || !*bad.IsError {
		t.Fatalf("bad = %#v", bad)
	}
	slow, err := Execute(context.Background(), root, ResultSpec{Kind: "mcp", Server: "slow", Tool: "probe", TimeoutS: 1})
	if err != nil {
		t.Fatal(err)
	}
	if slow.Failure != "timeout" {
		t.Fatalf("slow = %#v", slow)
	}
}

func TestMCPExpectEndToEnd(t *testing.T) {
	root := t.TempDir()
	stub := copiedSelf(t)
	writeMCP(t, root, map[string]any{
		"structured": map[string]any{
			"type":    "stdio",
			"command": stub,
			"env":     map[string]any{"ARBITER_TEST_STUB": "1", "ARBITER_TEST_MODE": "structured"},
		},
		"bad-structured": map[string]any{
			"type":    "stdio",
			"command": stub,
			"env":     map[string]any{"ARBITER_TEST_STUB": "1", "ARBITER_TEST_MODE": "bad-structured"},
		},
	})

	pass, err := Execute(context.Background(), root, ResultSpec{
		Kind: "mcp", Server: "structured", Tool: "probe",
		Expect: mustRaw(t, `[
			{"path":"ok","op":"eq","value":true},
			{"path":"summary.finding_count","op":"le","value":5},
			{"path":"checks.0.ok","op":"eq","value":true},
			{"path":"state","op":"exists"}
		]`),
	})
	if err != nil {
		t.Fatal(err)
	}
	if !Pass(pass) || pass.Verdict == nil || !*pass.Verdict {
		t.Fatalf("pass = %#v", pass)
	}
	if len(pass.ExpectReport) != 4 {
		t.Fatalf("expect_report len = %d, want 4", len(pass.ExpectReport))
	}

	// 假checkmate 同形回归:isError=false 而 overall=failed 不可满足 eq passed。
	checkmate, err := Execute(context.Background(), root, ResultSpec{
		Kind: "mcp", Server: "structured", Tool: "probe",
		Expect: mustRaw(t, `[{"path":"overall","op":"eq","value":"passed"}]`),
	})
	if err != nil {
		t.Fatal(err)
	}
	if Pass(checkmate) || checkmate.Verdict == nil || *checkmate.Verdict {
		t.Fatalf("checkmate = %#v", checkmate)
	}
	if len(checkmate.ExpectReport) != 1 || checkmate.ExpectReport[0].Actual != "failed" {
		t.Fatalf("expect_report = %+v", checkmate.ExpectReport)
	}

	// isError=true 即整体失败,纵使子句全部成立。
	gated, err := Execute(context.Background(), root, ResultSpec{
		Kind: "mcp", Server: "bad-structured", Tool: "probe",
		Expect: mustRaw(t, `[{"path":"ok","op":"eq","value":true}]`),
	})
	if err != nil {
		t.Fatal(err)
	}
	if Pass(gated) || gated.Verdict == nil || *gated.Verdict {
		t.Fatalf("gated = %#v", gated)
	}

	// 无 expect 时维持 legacy isError 语义,绝不产生类型化判定。
	legacy, err := Execute(context.Background(), root, ResultSpec{
		Kind: "mcp", Server: "structured", Tool: "probe",
	})
	if err != nil {
		t.Fatal(err)
	}
	if !Pass(legacy) || legacy.Verdict != nil {
		t.Fatalf("legacy = %#v", legacy)
	}
}

func TestMCPEntryEnvOverridesInherited(t *testing.T) {
	// embedded 引擎条目靠 env 携带 PYTHONPATH;条目值必须压过继承环境
	// (glibc getenv 取首个匹配,简单 append 会让继承值遮蔽条目值)。
	root := t.TempDir()
	stub := copiedSelf(t)
	t.Setenv("ARBITER_TEST_MODE", "bad")
	writeMCP(t, root, map[string]any{
		"ok": map[string]any{
			"type":    "stdio",
			"command": stub,
			"env":     map[string]any{"ARBITER_TEST_STUB": "1", "ARBITER_TEST_MODE": "ok"},
		},
	})
	result, err := Execute(context.Background(), root, ResultSpec{Kind: "mcp", Server: "ok", Tool: "probe"})
	if err != nil {
		t.Fatal(err)
	}
	if !Pass(result) {
		t.Fatalf("entry env did not override inherited env: %#v", result)
	}
}

func TestMCPPreflightErrors(t *testing.T) {
	root := t.TempDir()
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	writeMCP(t, root, map[string]any{
		"http": map[string]any{"type": "http", "command": self},
		"self": map[string]any{"type": "stdio", "command": self},
	})
	cases := []struct {
		name string
		spec ResultSpec
		code string
	}{
		{"missing", ResultSpec{Kind: "mcp", Server: "missing", Tool: "probe"}, playbook.CodeServerNotFound},
		{"transport", ResultSpec{Kind: "mcp", Server: "http", Tool: "probe"}, playbook.CodeUnsupportedTransport},
		{"reserved", ResultSpec{Kind: "mcp", Server: "self", Tool: "probe"}, playbook.CodeReservedServer},
		{"bad result", ResultSpec{Kind: "mcp", Server: "self"}, playbook.CodeBadResult},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := Execute(context.Background(), root, tc.spec)
			se, ok := err.(*SpecError)
			if !ok || se.Code != tc.code {
				t.Fatalf("err = %#v want %s", err, tc.code)
			}
		})
	}
}

func TestMCPReservedServerAdversarialMatrix(t *testing.T) {
	root := t.TempDir()
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	bin := filepath.Join(root, "bin")
	if err := os.MkdirAll(bin, 0o755); err != nil {
		t.Fatal(err)
	}
	symlink := filepath.Join(bin, "arbiter-self-symlink")
	if err := os.Symlink(self, symlink); err != nil {
		t.Fatal(err)
	}
	hardlink := filepath.Join(bin, "arbiter-self-hardlink")
	if err := os.Link(self, hardlink); err != nil {
		t.Skipf("hardlink unsupported on this filesystem: %v", err)
	}
	pathName := "arbiter-self-path"
	if err := os.Symlink(self, filepath.Join(bin, pathName)); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))

	writeMCP(t, root, map[string]any{
		"direct":      map[string]any{"type": "stdio", "command": self},
		"symlink":     map[string]any{"type": "stdio", "command": symlink},
		"path_lookup": map[string]any{"type": "stdio", "command": pathName},
		"hardlink":    map[string]any{"type": "stdio", "command": hardlink},
		"argv":        map[string]any{"type": "stdio", "command": self, "args": []any{"--not-a-bypass"}},
		"foreign_arg": map[string]any{"type": "stdio", "command": "/bin/echo", "args": []any{self}},
	})

	for _, name := range []string{"direct", "symlink", "path_lookup", "hardlink", "argv"} {
		t.Run(name, func(t *testing.T) {
			_, err := readServerConfig(root, name)
			if code := specCode(err); code != playbook.CodeReservedServer {
				t.Fatalf("code = %q, want %q (err=%v)", code, playbook.CodeReservedServer, err)
			}
		})
	}
	t.Run("foreign command with self argument is not self", func(t *testing.T) {
		if _, err := readServerConfig(root, "foreign_arg"); err != nil {
			t.Fatalf("foreign arg rejected: %v", err)
		}
	})
}

func runStub() {
	server := mcp.NewServer(&mcp.Implementation{Name: "stub", Version: "v1"}, nil)
	server.AddTool(&mcp.Tool{Name: "probe", InputSchema: map[string]any{"type": "object"}}, func(ctx context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		switch os.Getenv("ARBITER_TEST_MODE") {
		case "bad":
			return &mcp.CallToolResult{IsError: true, Content: []mcp.Content{&mcp.TextContent{Text: "bad"}}}, nil
		case "slow":
			time.Sleep(3 * time.Second)
		case "structured":
			return &mcp.CallToolResult{
				Content:           []mcp.Content{&mcp.TextContent{Text: "structured"}},
				StructuredContent: stubStructured(),
			}, nil
		case "bad-structured":
			return &mcp.CallToolResult{
				IsError:           true,
				Content:           []mcp.Content{&mcp.TextContent{Text: "bad structured"}},
				StructuredContent: stubStructured(),
			}, nil
		}
		return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: "ok"}}}, nil
	})
	if err := server.Run(context.Background(), &mcp.StdioTransport{}); err != nil {
		os.Exit(2)
	}
	os.Exit(0)
}

// stubStructured 模拟伙伴诊断服务器(gdb-mcp/perf-mcp)的 structuredContent 形状。
func stubStructured() map[string]any {
	return map[string]any{
		"ok":      true,
		"state":   "stopped",
		"overall": "failed",
		"summary": map[string]any{"finding_count": 2, "all_successful": true},
		"checks":  []any{map[string]any{"name": "gdb", "ok": true}},
	}
}

func writeMCP(t *testing.T, root string, servers map[string]any) {
	t.Helper()
	data, err := json.Marshal(map[string]any{"mcpServers": servers})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(deploy.MCPConfigPath(root), data, 0o644); err != nil {
		t.Fatal(err)
	}
}

func copiedSelf(t *testing.T) string {
	t.Helper()
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(self)
	if err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(t.TempDir(), "stub")
	if err := os.WriteFile(target, data, 0o755); err != nil {
		t.Fatal(err)
	}
	return target
}
