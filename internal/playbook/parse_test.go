package playbook

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const validBook = `---
name: hotfix-verify
description: 修复构建失败并验证回归的标准流程。适用于 CI 红灯、编译报错场景。
---

[STEP] diagnose
[StepJob]
定位构建失败的直接原因。
[CheckList]
- 产出失败根因结论与证据文件路径
- 确认失败可在本地复现
[Branch]
success: fix
failure: diagnose

[STEP] fix
[StepJob]
按上一步结论实施最小修复。
[CheckList]
- 完成修复且构建通过
[Branch]
success: END
failure: diagnose
`

func TestParseValidBook(t *testing.T) {
	book, issues := ParseBytes("valid.md", []byte(validBook))
	if len(issues) != 0 {
		t.Fatalf("issues = %#v", issues)
	}
	if book.Name != "hotfix-verify" || book.Entry != "diagnose" {
		t.Fatalf("unexpected book: %#v", book)
	}
	if got := book.Steps["diagnose"].Branch.Success; got != "fix" {
		t.Fatalf("success target = %q", got)
	}
	if len(book.OrderedSteps()) != 2 {
		t.Fatalf("ordered steps = %d", len(book.OrderedSteps()))
	}
}

func TestParseCapabilitiesVerifyAndTypedGoal(t *testing.T) {
	body := `---
name: typed-opening
description: uses named predicates
capabilities: [recipes]
---

[Verify] repro-passes
run: unit
tests: ["Suite.Case"]
expect: {"overall":"passed","min_passed":1}

[SetGoal]
fact: callers:main
expect: {"min_results":1,"complete":true}

[STEP] fix
[StepJob]
Make the smallest code change.
[CheckList]
- Submit repro-passes
[Branch]
success: END
failure: fix
`
	book, issues := ParseBytes("typed.md", []byte(body))
	if len(issues) != 0 {
		t.Fatalf("issues = %#v", issues)
	}
	if got := strings.Join(book.Capabilities, ","); got != "recipes" {
		t.Fatalf("capabilities = %#v", book.Capabilities)
	}
	spec, ok := book.Verify["repro-passes"]
	if !ok {
		t.Fatalf("verify map = %#v", book.Verify)
	}
	if spec.Kind != "run" || spec.Recipe != "unit" || len(spec.Tests) != 1 || spec.Tests[0] != "Suite.Case" {
		t.Fatalf("verify spec = %#v", spec)
	}
	if string(spec.Expect) != `{"overall":"passed","min_passed":1}` {
		t.Fatalf("verify expect = %s", spec.Expect)
	}
	if book.Goal == nil || book.Goal.Kind != "fact" || book.Goal.Query != "callers:main" {
		t.Fatalf("goal = %#v", book.Goal)
	}
}

func TestParseVerifyIssues(t *testing.T) {
	cases := []struct {
		name string
		body string
	}{
		{
			name: "bad capability",
			body: `---
name: n
description: d
capabilities: ["bad/slash"]
---
[STEP] a
[StepJob]
job
[CheckList]
- item
[Branch]
success: END
failure: END
`,
		},
		{
			name: "bad verify name",
			body: `---
name: n
description: d
---
[Verify] bad/name
shell: true
[STEP] a
[StepJob]
job
[CheckList]
- item
[Branch]
success: END
failure: END
`,
		},
		{
			name: "duplicate verify",
			body: `---
name: n
description: d
---
[Verify] repro
shell: true
[Verify] repro
shell: true
[STEP] a
[StepJob]
job
[CheckList]
- item
[Branch]
success: END
failure: END
`,
		},
		{
			name: "missing verify kind",
			body: `---
name: n
description: d
---
[Verify] repro
timeout_s: 10
[STEP] a
[StepJob]
job
[CheckList]
- item
[Branch]
success: END
failure: END
`,
		},
		{
			name: "run verify without recipe",
			body: `---
name: n
description: d
---
[Verify] repro
run:
tests: ["Suite.Case"]
expect: {"overall":"passed"}
[STEP] a
[StepJob]
job
[CheckList]
- item
[Branch]
success: END
failure: END
`,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, issues := ParseBytes("bad.md", []byte(tc.body))
			if !hasIssue(issues, IssueBadVerify) {
				t.Fatalf("issues = %#v, want %s", issues, IssueBadVerify)
			}
		})
	}
}

func TestParseIssues(t *testing.T) {
	cases := []struct {
		name string
		body string
		code string
	}{
		{"bad frontmatter", `[STEP] x`, IssueBadFrontmatter},
		{"no steps", `---
name: n
description: d
---
`, IssueNoSteps},
		{"missing section", `---
name: n
description: d
---
[STEP] a
[StepJob]
job
[CheckList]
- item
`, IssueMissingSection},
		{"empty job", `---
name: n
description: d
---
[STEP] a
[StepJob]

[CheckList]
- item
[Branch]
success: END
failure: END
`, IssueEmptyJob},
		{"empty list", `---
name: n
description: d
---
[STEP] a
[StepJob]
job
[CheckList]
[Branch]
success: END
failure: END
`, IssueEmptyChecklist},
		{"bad branch", `---
name: n
description: d
---
[STEP] a
[StepJob]
job
[CheckList]
- item
[Branch]
success: END
other: END
`, IssueBadBranch},
		{"unknown target", `---
name: n
description: d
---
[STEP] a
[StepJob]
job
[CheckList]
- item
[Branch]
success: b
failure: END
`, IssueUnknownBranchTarget},
		{"duplicate step", `---
name: n
description: d
---
[STEP] a
[StepJob]
job
[CheckList]
- item
[Branch]
success: END
failure: END
[STEP] a
[StepJob]
job
[CheckList]
- item
[Branch]
success: END
failure: END
`, IssueDuplicateStep},
		{"stray", `---
name: n
description: d
---
stray
`, IssueStrayContent},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, issues := ParseBytes("bad.md", []byte(tc.body))
			if !hasIssue(issues, tc.code) {
				t.Fatalf("issues = %#v, want %s", issues, tc.code)
			}
		})
	}
}

func TestParseOversize(t *testing.T) {
	data := make([]byte, MaxPlaybookBytes+1)
	_, issues := ParseBytes("large.md", data)
	if !hasIssue(issues, IssueOversize) {
		t.Fatalf("issues = %#v", issues)
	}
}

func TestScanDirNameConflict(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "a.md"), []byte(validBook), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "b.md"), []byte(validBook), 0o644); err != nil {
		t.Fatal(err)
	}
	cat := ScanDir(dir)
	if len(cat.Entries) != 2 {
		t.Fatalf("entries = %d", len(cat.Entries))
	}
	if !hasIssue(cat.Invalid, IssueNameConflict) {
		t.Fatalf("invalid = %#v", cat.Invalid)
	}
	if _, code := cat.Find("hotfix-verify"); code != CodeNameConflict {
		t.Fatalf("Find code = %q", code)
	}
}

const oneStepTail = `[STEP] a
[StepJob]
job
[CheckList]
- item
[Branch]
success: END
failure: END
`

func TestParseVerifyPolicy(t *testing.T) {
	verifySection := "[Verify] pass\nshell: exit 0\n\n"
	cases := []struct {
		name      string
		policy    string
		verify    bool
		wantBook  string
		wantIssue string
	}{
		{name: "default open", policy: "", verify: true, wantBook: ""},
		{name: "explicit open", policy: "verify_policy: open\n", verify: true, wantBook: "open"},
		{name: "named", policy: "verify_policy: named\n", verify: true, wantBook: "named"},
		{name: "invalid value", policy: "verify_policy: closed\n", verify: true, wantIssue: IssueBadFrontmatter},
		{name: "named without verify", policy: "verify_policy: named\n", verify: false, wantIssue: IssueBadFrontmatter},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			body := "---\nname: n\ndescription: d\n" + tc.policy + "---\n\n"
			if tc.verify {
				body += verifySection
			}
			body += oneStepTail
			book, issues := ParseBytes("p.md", []byte(body))
			if tc.wantIssue != "" {
				if !hasIssue(issues, tc.wantIssue) {
					t.Fatalf("issues = %#v, want %s", issues, tc.wantIssue)
				}
				return
			}
			if len(issues) != 0 {
				t.Fatalf("issues = %#v", issues)
			}
			if book.VerifyPolicy != tc.wantBook {
				t.Fatalf("verify_policy = %q, want %q", book.VerifyPolicy, tc.wantBook)
			}
		})
	}
}

func TestParseStepSubmit(t *testing.T) {
	head := "---\nname: n\ndescription: d\n---\n\n[Verify] pass\nshell: exit 0\n\n"
	step := func(submit string) string {
		s := "[STEP] a\n[StepJob]\njob\n[CheckList]\n- item\n"
		if submit != "" {
			s += submit + "\n"
		}
		return s + "[Branch]\nsuccess: END\nfailure: END\n"
	}

	// 绑定到已存在的 [Verify]:Step.Submit 落位,无 issue。
	book, issues := ParseBytes("p.md", []byte(head+step("[Submit] pass")))
	if len(issues) != 0 {
		t.Fatalf("valid [Submit] issues = %#v", issues)
	}
	if book.Steps["a"].Submit != "pass" {
		t.Fatalf("Submit = %q, want pass", book.Steps["a"].Submit)
	}

	cases := []struct {
		name string
		body string
		want string
	}{
		{"unknown verify", head + step("[Submit] nope"), IssueBadSubmit},
		{"invalid name", head + step("[Submit] not a name"), IssueBadSubmit},
		{"duplicate", head + step("[Submit] pass\n[Submit] pass"), IssueBadSubmit},
		{"outside a step", "---\nname: n\ndescription: d\n---\n\n[Submit] pass\n\n" + step(""), IssueStrayContent},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, issues := ParseBytes("p.md", []byte(tc.body))
			if !hasIssue(issues, tc.want) {
				t.Fatalf("issues = %#v, want %s", issues, tc.want)
			}
		})
	}
}

func TestParseCheckpoint(t *testing.T) {
	head := "---\nname: n\ndescription: d\n---\n\n"
	step := func(mid string) string {
		return head + "[STEP] a\n[StepJob]\njob\n" + mid + "[Branch]\nsuccess: END\nfailure: a\n"
	}

	// Valid checkpoint step: question captured, no [CheckList] needed.
	book, issues := ParseBytes("p.md", []byte(step("[Checkpoint]\nApprove the draft?\n")))
	if len(issues) != 0 {
		t.Fatalf("valid checkpoint issues = %#v", issues)
	}
	if book.Steps["a"].Checkpoint != "Approve the draft?" {
		t.Fatalf("Checkpoint = %q", book.Steps["a"].Checkpoint)
	}

	cases := []struct {
		name string
		body string
		want string
	}{
		{"both list and checkpoint", step("[CheckList]\n- x\n[Checkpoint]\nq?\n"), IssueBadCheckpoint},
		{"neither", step(""), IssueMissingSection},
		{"empty question", step("[Checkpoint]\n"), IssueBadCheckpoint},
		{"checkpoint cannot bind submit", "---\nname: n\ndescription: d\n---\n\n[Verify] v\nshell: exit 0\n\n[STEP] a\n[StepJob]\nj\n[Checkpoint]\nq?\n[Submit] v\n[Branch]\nsuccess: END\nfailure: a\n", IssueBadCheckpoint},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, issues := ParseBytes("p.md", []byte(tc.body))
			if !hasIssue(issues, tc.want) {
				t.Fatalf("issues = %#v, want %s", issues, tc.want)
			}
		})
	}
}

func TestParseAllowOverrides(t *testing.T) {
	body := "---\nname: n\ndescription: d\n---\n\n" +
		"[Verify] suite\nrun: unit\ntests: [\"*\"]\nexpect: {\"overall\":\"passed\"}\nallow_overrides: [\"tests\", \"options\"]\n\n" +
		oneStepTail
	book, issues := ParseBytes("a.md", []byte(body))
	if len(issues) != 0 {
		t.Fatalf("issues = %#v", issues)
	}
	if got := strings.Join(book.Verify["suite"].AllowOverrides, ","); got != "tests,options" {
		t.Fatalf("allow_overrides = %#v", book.Verify["suite"].AllowOverrides)
	}
}

func TestParseAllowOverridesIssues(t *testing.T) {
	runHead := "[Verify] suite\nrun: unit\ntests: [\"*\"]\nexpect: {\"overall\":\"passed\"}\n"
	cases := []struct {
		name string
		body string
		code string
	}{
		{"illegal value", runHead + "allow_overrides: [\"expect\"]\n", IssueBadVerify},
		{"not json", runHead + "allow_overrides: tests\n", IssueBadVerify},
		{"duplicate entry", runHead + "allow_overrides: [\"tests\", \"tests\"]\n", IssueBadVerify},
		{"non-run kind", "[Verify] sh\nshell: exit 0\nallow_overrides: [\"tests\"]\n", IssueBadVerify},
		{"in setgoal", "[SetGoal]\nshell: exit 0\nallow_overrides: [\"tests\"]\n", IssueBadGoal},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			body := "---\nname: n\ndescription: d\n---\n\n" + tc.body + "\n" + oneStepTail
			_, issues := ParseBytes("a.md", []byte(body))
			if !hasIssue(issues, tc.code) {
				t.Fatalf("issues = %#v, want %s", issues, tc.code)
			}
		})
	}
}

func TestParseVerifyCannotReferenceVerify(t *testing.T) {
	body := "---\nname: n\ndescription: d\n---\n\n" +
		"[Verify] one\nshell: exit 0\n\n[Verify] two\nverify: one\n\n" + oneStepTail
	_, issues := ParseBytes("v.md", []byte(body))
	if !hasIssue(issues, IssueBadVerify) {
		t.Fatalf("issues = %#v, want %s", issues, IssueBadVerify)
	}
	found := false
	for _, issue := range issues {
		if strings.Contains(issue.Detail, "verify cannot reference verify") {
			found = true
		}
	}
	if !found {
		t.Fatalf("issues = %#v, want detail naming the rule", issues)
	}
}

func TestParseFullLineComments(t *testing.T) {
	body := "---\nname: n\ndescription: d\n---\n\n" +
		"[Verify] pass\n# leading comment\nshell: exit 0\n  # indented comment\ntimeout_s: 30\n\n" +
		"[SetGoal]\n# goal comment\nfact: symbol:Foo\nexpect: {\"min_results\":1}\n\n" +
		oneStepTail
	book, issues := ParseBytes("c.md", []byte(body))
	if len(issues) != 0 {
		t.Fatalf("issues = %#v", issues)
	}
	spec := book.Verify["pass"]
	if spec.Kind != "shell" || spec.Command != "exit 0" || spec.TimeoutS != 30 {
		t.Fatalf("verify spec = %#v", spec)
	}
	if book.Goal == nil || book.Goal.Kind != "fact" || book.Goal.Query != "symbol:Foo" {
		t.Fatalf("goal = %#v", book.Goal)
	}
}

func TestParseRunRecipeCharset(t *testing.T) {
	const hint = "inline '#' comments are not supported"
	bad := []struct {
		name   string
		recipe string
		hinted bool
	}{
		{"inline comment", "unit # prod", true},
		{"path escape", "../evil", false},
		{"dotdot", "a..b", false},
		{"leading dot", ".hidden", false},
		{"slash", "dir/unit", false},
	}
	for _, tc := range bad {
		t.Run(tc.name, func(t *testing.T) {
			body := "---\nname: n\ndescription: d\n---\n\n" +
				"[Verify] r\nrun: " + tc.recipe + "\ntests: [\"*\"]\nexpect: {\"overall\":\"passed\"}\n\n" + oneStepTail
			_, issues := ParseBytes("r.md", []byte(body))
			if !hasIssue(issues, IssueBadVerify) {
				t.Fatalf("issues = %#v, want %s", issues, IssueBadVerify)
			}
			detail := ""
			for _, issue := range issues {
				if issue.Code == IssueBadVerify && strings.Contains(issue.Detail, "[A-Za-z0-9_-][A-Za-z0-9._-]*") {
					detail = issue.Detail
				}
			}
			if detail == "" {
				t.Fatalf("issues = %#v, want charset named", issues)
			}
			if strings.Contains(detail, hint) != tc.hinted {
				t.Fatalf("detail = %q, hint expected=%t", detail, tc.hinted)
			}
		})
	}
	good := "---\nname: n\ndescription: d\n---\n\n" +
		"[Verify] r\nrun: unit-v2.1_x\ntests: [\"*\"]\nexpect: {\"overall\":\"passed\"}\n\n" + oneStepTail
	if _, issues := ParseBytes("r.md", []byte(good)); len(issues) != 0 {
		t.Fatalf("good recipe id issues = %#v", issues)
	}
}

func TestParseFactCommentTerm(t *testing.T) {
	body := "---\nname: n\ndescription: d\n---\n\n" +
		"[Verify] f\nfact: symbol:foo # bar\nexpect: {\"min_results\":1}\n\n" + oneStepTail
	_, issues := ParseBytes("f.md", []byte(body))
	if !hasIssue(issues, IssueBadVerify) {
		t.Fatalf("issues = %#v, want %s", issues, IssueBadVerify)
	}
	found := false
	for _, issue := range issues {
		if strings.Contains(issue.Detail, "fact query term '#' cannot match any symbol") &&
			strings.Contains(issue.Detail, "inline '#' comments are not supported") {
			found = true
		}
	}
	if !found {
		t.Fatalf("issues = %#v, want term rejection with hint", issues)
	}

	// 词中混入 # 的病态查询仍然合法:按语法拒绝,不做启发式猜测。
	embedded := "---\nname: n\ndescription: d\n---\n\n" +
		"[Verify] f\nfact: path:a#b\nexpect: {\"min_results\":1}\n\n" + oneStepTail
	if _, issues := ParseBytes("f.md", []byte(embedded)); len(issues) != 0 {
		t.Fatalf("embedded # issues = %#v", issues)
	}
}

func TestParseCommentHints(t *testing.T) {
	const hint = "inline '#' comments are not supported (use full-line comments)"
	cases := []struct {
		name string
		line string
	}{
		{"expect", "fact: q\nexpect: {\"min_results\":1} # note"},
		{"arguments", "mcp: s t\narguments: {} # note"},
		{"tests", "run: unit\ntests: [\"*\"] # note\nexpect: {\"overall\":\"passed\"}"},
		{"options", "run: unit\ntests: [\"*\"]\nexpect: {\"overall\":\"passed\"}\noptions: {} # note"},
		{"timeout_s", "shell: exit 0\ntimeout_s: 30 # note"},
		{"output_lines", "shell: exit 0\noutput_lines: 10 # note"},
		{"mcp", "mcp: s t # note"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			body := "---\nname: n\ndescription: d\n---\n\n[Verify] v\n" + tc.line + "\n\n" + oneStepTail
			_, issues := ParseBytes("h.md", []byte(body))
			found := false
			for _, issue := range issues {
				if issue.Code == IssueBadVerify && strings.HasSuffix(issue.Detail, hint) {
					found = true
				}
			}
			if !found {
				t.Fatalf("issues = %#v, want %s detail ending with hint", issues, IssueBadVerify)
			}
		})
	}
	// 不含 # 的同类失败不携带提示。
	body := "---\nname: n\ndescription: d\n---\n\n[Verify] v\nshell: exit 0\ntimeout_s: bogus\n\n" + oneStepTail
	_, issues := ParseBytes("h.md", []byte(body))
	for _, issue := range issues {
		if strings.Contains(issue.Detail, "inline '#'") {
			t.Fatalf("unexpected hint without '#': %#v", issues)
		}
	}
	if !hasIssue(issues, IssueBadVerify) {
		t.Fatalf("issues = %#v", issues)
	}
}

func TestParseRejectsNonASCIIIdentifier(t *testing.T) {
	// FORMAT.md:122 limits names to ASCII [A-Za-z0-9_-]+; non-ASCII letters
	// (homoglyphs/full-width) the wider Unicode rule once admitted must reject.
	cases := []struct {
		name string
		body string
	}{
		{
			"verify name",
			"---\nname: n\ndescription: d\n---\n\n[Verify] café\nshell: exit 0\n\n" + oneStepTail,
		},
		{
			"capability",
			"---\nname: n\ndescription: d\ncapabilities: [\"ＡＢＣ\"]\n---\n\n" + oneStepTail,
		},
		{
			"verify ref in setgoal",
			"---\nname: n\ndescription: d\n---\n\n[SetGoal]\nverify: café\n\n" + oneStepTail,
		},
		{
			"submit name",
			"---\nname: n\ndescription: d\n---\n\n[Verify] pass\nshell: exit 0\n\n" +
				"[STEP] a\n[StepJob]\njob\n[CheckList]\n- item\n[Submit] café\n[Branch]\nsuccess: END\nfailure: END\n",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, issues := ParseBytes("u.md", []byte(tc.body))
			if len(issues) == 0 {
				t.Fatalf("non-ASCII identifier %s accepted, want rejection", tc.name)
			}
		})
	}
}

func TestOrderedStepsDeterministicFallback(t *testing.T) {
	// A Playbook rehydrated from match-state JSON loses the unexported `order`
	// field; the fallback must iterate Steps by sorted id, not raw map order.
	book := Playbook{Steps: map[string]Step{
		"c": {ID: "c"},
		"a": {ID: "a"},
		"b": {ID: "b"},
	}}
	for i := 0; i < 32; i++ {
		got := book.OrderedSteps()
		if len(got) != 3 || got[0].ID != "a" || got[1].ID != "b" || got[2].ID != "c" {
			t.Fatalf("ordered steps = %#v, want a,b,c", got)
		}
	}
}

func hasIssue(issues []Issue, code string) bool {
	for _, issue := range issues {
		if issue.Code == code {
			return true
		}
	}
	return false
}
