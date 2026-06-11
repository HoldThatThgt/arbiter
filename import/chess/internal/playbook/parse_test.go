package playbook

import (
	"os"
	"path/filepath"
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

func hasIssue(issues []Issue, code string) bool {
	for _, issue := range issues {
		if issue.Code == code {
			return true
		}
	}
	return false
}
