package guard

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func event(t *testing.T, tool string, input map[string]any) []byte {
	t.Helper()
	raw, err := json.Marshal(map[string]any{"tool_name": tool, "tool_input": input})
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestGuardDecisions(t *testing.T) {
	root := "/repo"
	cases := []struct {
		name  string
		tool  string
		input map[string]any
		deny  bool
		hint  string
	}{
		// 文件类工具:相对与绝对路径都拦。
		{"read playbook rel", "Read", map[string]any{"file_path": ".arbiter/playbook/freeplay.md"}, true, "ShowStepJob"},
		{"read playbook abs", "Read", map[string]any{"file_path": "/repo/.arbiter/playbook/freeplay.md"}, true, "ShowStepJob"},
		{"edit match state", "Edit", map[string]any{"file_path": "/repo/.arbiter/match/run/state.json"}, true, "referee-owned"},
		{"write engine tree", "Write", map[string]any{"file_path": ".arbiter/engine/arbiter_engine/__init__.py"}, true, "digest-verified"},
		{"read seat agent", "Read", map[string]any{"file_path": ".claude/agents/arbiter-curator.md"}, true, "credential"},
		// Bash:命令文本里的字面出现。
		{"bash cat playbook", "Bash", map[string]any{"command": "cat .arbiter/playbook/gold-digger.md"}, true, "ShowStepJob"},
		{"bash abs journal", "Bash", map[string]any{"command": "tail -f /repo/.arbiter/match/log/journal.jsonl"}, true, "referee-owned"},
		{"bash agents glob", "Bash", map[string]any{"command": "grep key .claude/agents/arbiter-executor.md"}, true, "credential"},
		// Glob/Grep:path 与 pattern 双通道。
		{"grep playbook path", "Grep", map[string]any{"pattern": "verify", "path": ".arbiter/playbook"}, true, "ShowStepJob"},
		{"glob playbook pattern", "Glob", map[string]any{"pattern": ".arbiter/playbook/**"}, true, "ShowStepJob"},
		// 放行面:常规工作不受影响。
		{"read source", "Read", map[string]any{"file_path": "src/lock.c"}, false, ""},
		{"bash build", "Bash", map[string]any{"command": "make -j8 check"}, false, ""},
		{"bash arbiter status", "Bash", map[string]any{"command": "arbiter status --json"}, false, ""},
		{"grep source", "Grep", map[string]any{"pattern": "holdMask", "path": "src"}, false, ""},
		{"read recipes", "Read", map[string]any{"file_path": ".arbiter/recipes.yaml"}, false, ""},
		{"read config", "Read", map[string]any{"file_path": ".arbiter/config.yml"}, false, ""},
		// 非守备工具与畸形输入:fail-open。
		{"unknown tool", "WebFetch", map[string]any{"url": ".arbiter/playbook"}, false, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			decision := Decide(root, nil, event(t, tc.tool, tc.input))
			if decision.Deny != tc.deny {
				t.Fatalf("deny = %v, want %v (reason=%q)", decision.Deny, tc.deny, decision.Reason)
			}
			if tc.deny && !strings.Contains(decision.Reason, tc.hint) {
				t.Fatalf("reason %q missing teaching hint %q", decision.Reason, tc.hint)
			}
		})
	}
}

func TestGuardFailsOpenOnMalformedInput(t *testing.T) {
	for _, raw := range []string{"", "not json", `{"tool_name":"Read"}`, `{"tool_name":"Read","tool_input":"oops"}`} {
		if decision := Decide("/repo", nil, []byte(raw)); decision.Deny {
			t.Fatalf("malformed input must fail open: %q -> %+v", raw, decision)
		}
	}
}

// 注册测试只对改写类工具不可写;读、glob、grep、以及 Bash(编译/运行)放行。
func TestGuardFreezesRegisteredTests(t *testing.T) {
	root := "/repo"
	frozen := []string{"tests/repro_test.cc"}
	cases := []struct {
		name  string
		tool  string
		input map[string]any
		deny  bool
	}{
		{"edit frozen test", "Edit", map[string]any{"file_path": "tests/repro_test.cc"}, true},
		{"write frozen test", "Write", map[string]any{"file_path": "/repo/tests/repro_test.cc"}, true},
		{"edit other file", "Edit", map[string]any{"file_path": "src/bloom.cc"}, false},
		{"read frozen test", "Read", map[string]any{"file_path": "tests/repro_test.cc"}, false},
		{"grep frozen test", "Grep", map[string]any{"pattern": "x", "path": "tests/repro_test.cc"}, false},
		{"compile frozen test via bash", "Bash", map[string]any{"command": "g++ tests/repro_test.cc -o t"}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			decision := Decide(root, frozen, event(t, tc.tool, tc.input))
			if decision.Deny != tc.deny {
				t.Fatalf("deny = %v, want %v (reason=%q)", decision.Deny, tc.deny, decision.Reason)
			}
			if tc.deny && !strings.Contains(decision.Reason, "immutable") {
				t.Fatalf("frozen denial reason missing 'immutable': %q", decision.Reason)
			}
		})
	}
}

// 冻结判定不止于词法精确比对:指向冻结测试的符号链接别名、以及大小写不敏感卷
// 上的大小写变体,都解析到同一物理文件(os.SameFile),必须一并拒写。
func TestGuardFreezesCaseAndSymlinkVariants(t *testing.T) {
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "tests"), 0o755); err != nil {
		t.Fatal(err)
	}
	real := filepath.Join(root, "tests", "repro_test.cc")
	if err := os.WriteFile(real, []byte("x\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	frozen := []string{"tests/repro_test.cc"}

	// Exact path is denied even with no file on disk yet (lexical fast path).
	if d := Decide(root, frozen, event(t, "Edit", map[string]any{"file_path": "tests/repro_test.cc"})); !d.Deny {
		t.Fatal("exact frozen path not denied")
	}
	// An unrelated file stays writable.
	if d := Decide(root, frozen, event(t, "Edit", map[string]any{"file_path": "src/bloom.cc"})); d.Deny {
		t.Fatal("unrelated file denied")
	}

	// Symlink alias pointing at the frozen file → same inode → denied (cross-platform).
	alias := filepath.Join(root, "alias.cc")
	if err := os.Symlink(real, alias); err != nil {
		t.Skipf("symlink unsupported on this platform: %v", err)
	}
	if d := Decide(root, frozen, event(t, "Write", map[string]any{"file_path": "alias.cc"})); !d.Deny {
		t.Fatal("symlink alias to frozen test not denied")
	}

	// Case variant: denied only where the filesystem is case-insensitive
	// (macOS/Windows default). Detect via SameFile and assert accordingly.
	variant := filepath.Join(root, "tests", "Repro_Test.cc")
	caseInsensitive := false
	if a, err1 := os.Stat(real); err1 == nil {
		if b, err2 := os.Stat(variant); err2 == nil && os.SameFile(a, b) {
			caseInsensitive = true
		}
	}
	d := Decide(root, frozen, event(t, "Edit", map[string]any{"file_path": "tests/Repro_Test.cc"}))
	switch {
	case caseInsensitive && !d.Deny:
		t.Fatal("case-variant of frozen test not denied on case-insensitive FS")
	case !caseInsensitive && d.Deny:
		t.Fatal("case-variant denied on case-sensitive FS (it is a distinct file)")
	}
}

// A dangling symlink — one whose target (a frozen test) has been deleted via Bash —
// must still be denied: os.Stat follows-and-fails, so the readlink-target fallback
// catches a Write that would recreate/poison the frozen path through the alias.
func TestGuardFreezesDanglingSymlinkToFrozen(t *testing.T) {
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "tests"), 0o755); err != nil {
		t.Fatal(err)
	}
	frozen := []string{"tests/repro_test.cc"}
	// The frozen target is intentionally NOT created (simulating `rm` of the frozen test).
	alias := filepath.Join(root, "alias.cc")
	if err := os.Symlink(filepath.Join(root, "tests", "repro_test.cc"), alias); err != nil {
		t.Skipf("symlink unsupported on this platform: %v", err)
	}
	if d := Decide(root, frozen, event(t, "Write", map[string]any{"file_path": "alias.cc"})); !d.Deny {
		t.Fatal("dangling symlink to deleted frozen test not denied")
	}
}
