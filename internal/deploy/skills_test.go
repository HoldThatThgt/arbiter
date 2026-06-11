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

func TestArbiterIntroTemplateDefinesAdjudicatedBootstrap(t *testing.T) {
	text := mustTemplate("templates/arbiter-intro.md")
	for _, want := range []string{
		"adjudicated bootstrap match",
		"probe",
		"recipe-derivation",
		"register",
		`{"overall":{"one_of":["passed","failed"]}}`,
		"arbiter cc",
		"__SANITIZE_ADDRESS__",
		"__has_feature",
		"facts.key_flags",
		"proven-recipe count",
		"published snapshot",
	} {
		if !strings.Contains(text, want) {
			t.Fatalf("arbiter-intro template missing %q", want)
		}
	}
}

func TestInstrumentationMacroScanChecklist(t *testing.T) {
	root := t.TempDir()
	writeText(t, filepath.Join(root, "src", "asan.c"), "int x;\n#ifdef __SANITIZE_ADDRESS__\n#endif\n")
	writeText(t, filepath.Join(root, "src", "feature.c"), "#if __has_feature(thread_sanitizer)\n#endif\n")
	writeText(t, filepath.Join(root, "src", "near.c"), "int NOT__SANITIZE_ADDRESS__ = 0;\n")
	writeText(t, filepath.Join(root, ".arbiter", "derived.c"), "__SANITIZE_THREAD__\n")

	report, err := ScanInstrumentationMacros(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(report.Checklist) != 2 {
		t.Fatalf("checklist = %#v", report.Checklist)
	}
	got := []string{report.Checklist[0].Token, report.Checklist[1].Token}
	want := []string{"__SANITIZE_ADDRESS__", "__has_feature(thread_sanitizer)"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("tokens = %#v, want %#v", got, want)
	}
	if strings.Join(report.SuggestedKeyFlags, ",") != "-fsanitize=address,-fsanitize=thread" {
		t.Fatalf("key flags = %#v", report.SuggestedKeyFlags)
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
