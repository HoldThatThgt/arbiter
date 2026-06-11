package deploy

import (
	"path/filepath"
	"strings"
	"testing"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
)

func TestOpeningTemplateLint(t *testing.T) {
	book, issues := playbook.ParseBytes("freeplay.md", []byte(mustTemplate("templates/freeplay.md")))
	if len(issues) != 0 {
		t.Fatalf("freeplay issues = %#v", issues)
	}
	if book.Name != "freeplay" || book.Entry != "gear-up" {
		t.Fatalf("freeplay entry = %q name = %q", book.Entry, book.Name)
	}
	if _, ok := book.Verify["gear-up-published"]; !ok {
		t.Fatalf("verify predicates = %#v", book.Verify)
	}
}

func TestInitOpeningsWritesFreeplay(t *testing.T) {
	root := t.TempDir()
	opts := testInitOptions()
	opts.Openings = true
	if _, err := InitWithOptions(root, opts); err != nil {
		t.Fatal(err)
	}
	data := []byte(readText(t, filepath.Join(root, ".arbiter", "playbook", "freeplay.md")))
	book, issues := playbook.ParseBytes("freeplay.md", data)
	if len(issues) != 0 {
		t.Fatalf("freeplay issues = %#v", issues)
	}
	if book.Entry != "gear-up" {
		t.Fatalf("entry = %q", book.Entry)
	}
}

func TestPlaybookCreateScaffoldParsesAndStartsWithGearUp(t *testing.T) {
	scaffold := firstMarkdownFence(t, mustTemplate("templates/playbook-create.md"))
	book, issues := playbook.ParseBytes("scaffold.md", []byte(scaffold))
	if len(issues) != 0 {
		t.Fatalf("scaffold issues = %#v\n%s", issues, scaffold)
	}
	if book.Entry != "gear-up" {
		t.Fatalf("entry = %q", book.Entry)
	}
	if _, ok := book.Verify["gear-up-published"]; !ok {
		t.Fatalf("verify predicates = %#v", book.Verify)
	}
}

func TestArbiterPlayTemplateNamesFreeplayFallback(t *testing.T) {
	text := mustTemplate("templates/arbiter-play.md")
	for _, want := range []string{"freeplay", "fact-first", "CreateTask", "fact_refs"} {
		if !strings.Contains(text, want) {
			t.Fatalf("arbiter-play template missing %q", want)
		}
	}
}

func firstMarkdownFence(t *testing.T, text string) string {
	t.Helper()
	start := strings.Index(text, "```markdown\n")
	if start < 0 {
		t.Fatal("missing markdown fence")
	}
	start += len("```markdown\n")
	end := strings.Index(text[start:], "\n```")
	if end < 0 {
		t.Fatal("unterminated markdown fence")
	}
	return text[start : start+end]
}
