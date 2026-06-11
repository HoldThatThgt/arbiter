package playbook

import (
	"strings"
	"testing"
)

const notedBook = `---
name: noted
description: with gotcha section
---

[STEP] diagnose
[StepJob]
look around
[CheckList]
- root cause written down
[Gotcha]
- logs are rotated hourly
[Branch]
success: fix
failure: diagnose

[STEP] fix
[StepJob]
apply the fix
[CheckList]
- build green
[Branch]
success: END
failure: diagnose
`

func TestParseGotchaSection(t *testing.T) {
	book, issues := ParseBytes("noted.md", []byte(notedBook))
	if len(issues) != 0 {
		t.Fatalf("issues = %#v", issues)
	}
	if got := book.Steps["diagnose"].Gotchas; len(got) != 1 || got[0] != "logs are rotated hourly" {
		t.Fatalf("gotchas = %#v", got)
	}
	if got := book.Steps["fix"].Gotchas; len(got) != 0 {
		t.Fatalf("fix gotchas = %#v", got)
	}
	// 空 [Gotcha] 节合法(注记是可选辅助信息,不应让棋谱失效)
	empty := strings.Replace(notedBook, "- logs are rotated hourly\n", "", 1)
	if _, issues := ParseBytes("noted.md", []byte(empty)); len(issues) != 0 {
		t.Fatalf("empty section issues = %#v", issues)
	}
}

func TestParseGotchaIssues(t *testing.T) {
	cases := []struct {
		name string
		body string
	}{
		{"plain line in section", "[Gotcha]\nnot an item\n"},
		{"empty item", "[Gotcha]\n-\n"},
		{"trailing content on header", "[Gotcha] extra\n"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			body := "---\nname: x\ndescription: d\n---\n\n[STEP] only\n[StepJob]\nwork\n[CheckList]\n- done\n" +
				tc.body + "[Branch]\nsuccess: END\nfailure: only\n"
			_, issues := ParseBytes("x.md", []byte(body))
			if !hasIssue(issues, IssueStrayContent) {
				t.Fatalf("issues = %#v", issues)
			}
		})
	}
	// 节头出现在任何步骤之外 → stray
	body := "---\nname: x\ndescription: d\n---\n\n[Gotcha]\n\n[STEP] only\n[StepJob]\nwork\n[CheckList]\n- done\n[Branch]\nsuccess: END\nfailure: only\n"
	if _, issues := ParseBytes("x.md", []byte(body)); !hasIssue(issues, IssueStrayContent) {
		t.Fatalf("outside-step issues = %#v", issues)
	}
}

func TestAppendGotchaCreatesSection(t *testing.T) {
	out, ok := AppendGotcha([]byte(notedBook), "fix", "make clean first")
	if !ok {
		t.Fatal("step not found")
	}
	book, issues := ParseBytes("noted.md", out)
	if len(issues) != 0 {
		t.Fatalf("issues = %#v", issues)
	}
	if got := book.Steps["fix"].Gotchas; len(got) != 1 || got[0] != "make clean first" {
		t.Fatalf("gotchas = %#v", got)
	}
	if got := book.Steps["diagnose"].Gotchas; len(got) != 1 {
		t.Fatalf("diagnose gotchas changed: %#v", got)
	}
	if book.Steps["fix"].Job != "apply the fix" || len(book.Steps["fix"].Checklist) != 1 {
		t.Fatalf("fix step damaged: %#v", book.Steps["fix"])
	}
	// 新节挂在步骤内容末尾
	if !strings.Contains(string(out), "failure: diagnose\n\n[Gotcha]\n- make clean first\n") {
		t.Fatalf("placement:\n%s", out)
	}
}

func TestAppendGotchaAppendsToExistingSection(t *testing.T) {
	out, ok := AppendGotcha([]byte(notedBook), "diagnose", "second note")
	if !ok {
		t.Fatal("step not found")
	}
	book, issues := ParseBytes("noted.md", out)
	if len(issues) != 0 {
		t.Fatalf("issues = %#v", issues)
	}
	got := book.Steps["diagnose"].Gotchas
	if len(got) != 2 || got[0] != "logs are rotated hourly" || got[1] != "second note" {
		t.Fatalf("gotchas = %#v", got)
	}
	if !strings.Contains(string(out), "- logs are rotated hourly\n- second note\n[Branch]") {
		t.Fatalf("placement:\n%s", out)
	}
}

func TestAppendGotchaStepNotFound(t *testing.T) {
	if _, ok := AppendGotcha([]byte(notedBook), "ghost", "x"); ok {
		t.Fatal("expected not found")
	}
}

func TestAppendGotchaLastStepNoTrailingNewline(t *testing.T) {
	content := strings.TrimSuffix(notedBook, "\n")
	out, ok := AppendGotcha([]byte(content), "fix", "note at eof")
	if !ok {
		t.Fatal("step not found")
	}
	book, issues := ParseBytes("noted.md", out)
	if len(issues) != 0 {
		t.Fatalf("issues = %#v", issues)
	}
	if got := book.Steps["fix"].Gotchas; len(got) != 1 || got[0] != "note at eof" {
		t.Fatalf("gotchas = %#v", got)
	}
}

func TestAppendGotchaRepeatedAccumulates(t *testing.T) {
	content := []byte(notedBook)
	for i, note := range []string{"n1", "n2", "n3"} {
		next, ok := AppendGotcha(content, "fix", note)
		if !ok {
			t.Fatalf("round %d: step not found", i)
		}
		content = next
	}
	book, issues := ParseBytes("noted.md", content)
	if len(issues) != 0 {
		t.Fatalf("issues = %#v", issues)
	}
	got := book.Steps["fix"].Gotchas
	if len(got) != 3 || got[0] != "n1" || got[1] != "n2" || got[2] != "n3" {
		t.Fatalf("gotchas = %#v", got)
	}
}
