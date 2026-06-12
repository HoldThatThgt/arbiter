package guard

import (
	"encoding/json"
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
