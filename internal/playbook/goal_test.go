package playbook

import "testing"

const goalBook = `---
name: goalflow
description: goal flow
max_steps: 7
---

[SetGoal]
shell: exit 0
timeout_s: 30
output_lines: 10

[STEP] only
[StepJob]
work
[CheckList]
- done
[Branch]
success: only
failure: only
`

func TestParseGoalShell(t *testing.T) {
	book, issues := ParseBytes("g.md", []byte(goalBook))
	if len(issues) > 0 {
		t.Fatalf("issues = %#v", issues)
	}
	if book.MaxSteps != 7 || book.StepBudget() != 7 {
		t.Fatalf("max steps = %d", book.MaxSteps)
	}
	if book.Goal == nil || book.Goal.Kind != "shell" || book.Goal.Command != "exit 0" {
		t.Fatalf("goal = %#v", book.Goal)
	}
	if book.Goal.TimeoutS != 30 || book.Goal.OutputLines != 10 {
		t.Fatalf("goal limits = %#v", book.Goal)
	}
}

func TestParseGoalMCP(t *testing.T) {
	body := `---
name: g2
description: d
---

[SetGoal]
mcp: probe SomeTool
arguments: {"pr": 42}

[STEP] only
[StepJob]
work
[CheckList]
- done
[Branch]
success: END
failure: only
`
	book, issues := ParseBytes("g2.md", []byte(body))
	if len(issues) > 0 {
		t.Fatalf("issues = %#v", issues)
	}
	g := book.Goal
	if g == nil || g.Kind != "mcp" || g.Server != "probe" || g.Tool != "SomeTool" {
		t.Fatalf("goal = %#v", g)
	}
	if g.Arguments["pr"] != float64(42) {
		t.Fatalf("arguments = %#v", g.Arguments)
	}
}

func TestParseGoalAndBudgetIssues(t *testing.T) {
	cases := []struct {
		name string
		body string
		code string
	}{
		{"dup kind", "[SetGoal]\nshell: a\nmcp: s t\n", IssueBadGoal},
		{"no kind", "[SetGoal]\ntimeout_s: 5\n", IssueBadGoal},
		{"bad args json", "[SetGoal]\nmcp: s t\narguments: not-json\n", IssueBadGoal},
		{"args without mcp", "[SetGoal]\nshell: a\narguments: {}\n", IssueBadGoal},
		{"unknown key", "[SetGoal]\nshell: a\nwhat: x\n", IssueBadGoal},
		{"dup key", "[SetGoal]\nshell: a\nshell: b\n", IssueBadGoal},
		{"timeout range", "[SetGoal]\nshell: a\ntimeout_s: 999999\n", IssueBadGoal},
		{"goal after step", "[STEP] s\n[StepJob]\nx\n[CheckList]\n- a\n[Branch]\nsuccess: END\nfailure: END\n[SetGoal]\nshell: a\n", IssueBadGoal},
		{"second goal", "[SetGoal]\nshell: a\n\n[SetGoal]\nshell: b\n", IssueBadGoal},
		// 空 recipe 的 run 谓词会流入引擎 stub 分支并产出空洞 checkmate(#run-recipe)
		{"run without recipe", "[SetGoal]\nrun:\ntests: [\"a\"]\nexpect: {\"overall\":\"passed\"}\n", IssueBadGoal},
		// 以下三例与运行期 verify 校验对齐(internal/verify/typed.go)
		{"run expect empty object", "[SetGoal]\nrun: unit\ntests: [\"a\"]\nexpect: {}\n", IssueBadGoal},
		{"run tests empty entry", "[SetGoal]\nrun: unit\ntests: [\"\"]\nexpect: {\"overall\":\"passed\"}\n", IssueBadGoal},
		{"mcp expect not array", "[SetGoal]\nmcp: s t\nexpect: {\"path\":\"x\",\"op\":\"exists\"}\n", IssueBadGoal},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			body := "---\nname: x\ndescription: d\n---\n\n" + tc.body + `
[STEP] only
[StepJob]
work
[CheckList]
- done
[Branch]
success: END
failure: only
`
			_, issues := ParseBytes("x.md", []byte(body))
			if !hasIssueCode(issues, tc.code) {
				t.Fatalf("issues = %#v", issues)
			}
		})
	}
	_, issues := ParseBytes("y.md", []byte("---\nname: y\ndescription: d\nmax_steps: 99999\n---\n\n[STEP] s\n[StepJob]\nx\n[CheckList]\n- a\n[Branch]\nsuccess: END\nfailure: s\n"))
	if !hasIssueCode(issues, IssueBadMaxSteps) {
		t.Fatalf("max_steps issues = %#v", issues)
	}
}

func TestParseGoalVerifyAlias(t *testing.T) {
	verifySection := `[Verify] suite-green
run: unit
tests: ["*"]
expect: {"overall":"passed","max_failed":0}
allow_overrides: ["tests"]
`
	goalSection := `[SetGoal]
verify: suite-green
`
	step := `[STEP] only
[StepJob]
work
[CheckList]
- done
[Branch]
success: END
failure: only
`
	orders := map[string]string{
		"verify first": verifySection + "\n" + goalSection + "\n" + step,
		"goal first":   goalSection + "\n" + verifySection + "\n" + step,
	}
	for name, body := range orders {
		t.Run(name, func(t *testing.T) {
			book, issues := ParseBytes("g.md", []byte("---\nname: n\ndescription: d\n---\n\n"+body))
			if len(issues) != 0 {
				t.Fatalf("issues = %#v", issues)
			}
			g := book.Goal
			if g == nil || g.Kind != "run" || g.Recipe != "unit" {
				t.Fatalf("goal = %#v", g)
			}
			if len(g.Tests) != 1 || g.Tests[0] != "*" {
				t.Fatalf("goal tests = %#v", g.Tests)
			}
			if string(g.Expect) != `{"overall":"passed","max_failed":0}` {
				t.Fatalf("goal expect = %s", g.Expect)
			}
			// 解析后的 goal 是纯内联谓词:引用与 curator 专属字段都已清除。
			if g.Verify != "" || len(g.AllowOverrides) != 0 {
				t.Fatalf("goal not fully resolved: %#v", g)
			}
			// 深拷贝:goal 与具名谓词不共享底层存储。
			g.Tests[0] = "mutated"
			if book.Verify["suite-green"].Tests[0] != "*" {
				t.Fatalf("goal aliases named spec storage: %#v", book.Verify["suite-green"])
			}
		})
	}
}

func TestParseGoalVerifyAliasIssues(t *testing.T) {
	cases := []struct {
		name string
		body string
	}{
		{"unknown name", "[SetGoal]\nverify: missing\n"},
		{"verify then inline", "[SetGoal]\nverify: pass\nshell: exit 0\n"},
		{"inline then verify", "[SetGoal]\nshell: exit 0\nverify: pass\n"},
		{"verify with timeout", "[SetGoal]\nverify: pass\ntimeout_s: 30\n"},
		{"invalid reference", "[SetGoal]\nverify: bad/name\n"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			body := "---\nname: n\ndescription: d\n---\n\n[Verify] pass\nshell: exit 0\n\n" + tc.body + `
[STEP] only
[StepJob]
work
[CheckList]
- done
[Branch]
success: END
failure: only
`
			_, issues := ParseBytes("g.md", []byte(body))
			if !hasIssueCode(issues, IssueBadGoal) {
				t.Fatalf("issues = %#v, want %s", issues, IssueBadGoal)
			}
		})
	}
}

func hasIssueCode(issues []Issue, code string) bool {
	for _, issue := range issues {
		if issue.Code == code {
			return true
		}
	}
	return false
}
