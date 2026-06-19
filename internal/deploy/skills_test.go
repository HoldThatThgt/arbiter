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
	// freeplay 的前提就是不受约束的谓词,必须保持 open 策略(endgame 夹具靠它提交内联 shell)。
	if book.VerifyPolicy != "" {
		t.Fatalf("freeplay verify_policy = %q, want open default", book.VerifyPolicy)
	}
}

func TestInitOpeningsWritesFreeplay(t *testing.T) {
	root := t.TempDir()
	opts := testInitOptions()
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
	for _, name := range []string{"build-feature.md", "fix-reported-bug.md", "fix-slow-path.md", "hunt-latent-bugs.md", "gold-digger.md", "recipe-derivation.md", "regression-triage.md"} {
		if _, issues := playbook.ParseFile(filepath.Join(root, ".arbiter", "playbook", name)); len(issues) != 0 {
			t.Fatalf("missing or invalid %s: %#v", name, issues)
		}
	}
}

func TestBaseOpeningTemplatesParse(t *testing.T) {
	cases := []struct {
		file        string
		name        string
		entry       string
		capability  string
		policy      string
		verify      []string
		overridable []string
	}{
		{
			file:        "gold-digger.md",
			name:        "gold-digger",
			entry:       "gear-up",
			policy:      "named",
			verify:      []string{"gear-up-published", "repro-fails", "suite-green"},
			overridable: []string{"repro-fails"},
		},
		{
			file:       "recipe-derivation.md",
			name:       "recipe-derivation",
			entry:      "derive",
			capability: "recipes",
			policy:     "named",
			verify:     []string{"build-published", "candidate-proven", "tests-enumerated", "perf-static-scan", "perf-command-measured", "gdb-debugs-real-binary"},
		},
		{
			file:        "regression-triage.md",
			name:        "regression-triage",
			entry:       "gear-up",
			policy:      "named",
			verify:      []string{"gear-up-published", "regression-reproduced", "suite-green"},
			overridable: []string{"regression-reproduced"},
		},
		{
			file:        "openings/hunt-latent-bugs.md",
			name:        "hunt-latent-bugs",
			entry:       "hypothesize",
			verify:      []string{"symptom-proven"},
			overridable: []string{"symptom-proven"},
		},
		{
			file:        "openings/build-feature.md",
			name:        "build-feature",
			entry:       "scenario",
			policy:      "named",
			verify:      []string{"tests-fail", "suite-green"},
			overridable: []string{"tests-fail"},
		},
		{
			file:   "openings/fix-reported-bug.md",
			name:   "fix-reported-bug",
			entry:  "write-repro",
			policy: "named",
			verify: []string{"repro-runs-red", "suite-green"},
		},
		{
			file:        "openings/fix-slow-path.md",
			name:        "fix-slow-path",
			entry:       "write-ratio-test",
			policy:      "named",
			verify:      []string{"ratio-runs-red", "suite-green"},
			overridable: []string{"ratio-runs-red"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			book, issues := playbook.ParseBytes(tc.file, []byte(mustTemplate("templates/"+tc.file)))
			if len(issues) != 0 {
				t.Fatalf("%s issues = %#v", tc.file, issues)
			}
			if book.Name != tc.name || book.Entry != tc.entry {
				t.Fatalf("%s name/entry = %q/%q", tc.file, book.Name, book.Entry)
			}
			if tc.capability != "" && strings.Join(book.Capabilities, ",") != tc.capability {
				t.Fatalf("%s capabilities = %#v", tc.file, book.Capabilities)
			}
			if book.VerifyPolicy != tc.policy {
				t.Fatalf("%s verify_policy = %q, want %q", tc.file, book.VerifyPolicy, tc.policy)
			}
			for _, name := range tc.verify {
				if _, ok := book.Verify[name]; !ok {
					t.Fatalf("%s missing verify %q in %#v", tc.file, name, book.Verify)
				}
			}
			for _, name := range tc.overridable {
				if got := strings.Join(book.Verify[name].AllowOverrides, ","); got != "tests" {
					t.Fatalf("%s verify %q allow_overrides = %q, want tests", tc.file, name, got)
				}
			}
			// 起手棋谱(repo 无关)刻意不设 [SetGoal]:终局条件是走到 END,
			// 一个 suite-green goal 会在红测试出现前的第 1 回合就被误判为 checkmate。
			if tc.policy == "" && tc.capability == "" && book.Goal != nil {
				t.Fatalf("%s unexpectedly declares a goal: %#v", tc.file, book.Goal)
			}
		})
	}
	// regression-triage 的 goal 经 `verify: suite-green` 别名解析,内容与具名谓词逐字一致。
	book, issues := playbook.ParseBytes("regression-triage.md", []byte(mustTemplate("templates/regression-triage.md")))
	if len(issues) != 0 {
		t.Fatalf("regression-triage issues = %#v", issues)
	}
	goal := book.Goal
	if goal == nil || goal.Kind != "run" || goal.Recipe != "src_compile" {
		t.Fatalf("regression-triage goal = %#v", goal)
	}
	if string(goal.Expect) != string(book.Verify["suite-green"].Expect) {
		t.Fatalf("goal expect %s != suite-green expect %s", goal.Expect, book.Verify["suite-green"].Expect)
	}

	// recipe-derivation no longer sets an early [SetGoal]. The old goal (tests-enumerated)
	// checkmated the match the instant facts published — at the derive step — which skipped the
	// reconciliation steps entirely. The match now runs derive → prove → enumerate →
	// reconcile-perf → reconcile-diag → confirm → END, binding a referee-verified predicate to
	// every gated step so every wired surface is proven on its REAL function before END (not a
	// version probe). tests-enumerated survives as the enumerate step's bound predicate, so the
	// referee still re-queries the published _Test index itself (libclang records each gtest case
	// as its generated Suite_Name_Test fixture TYPE, which only a published snapshot carries; no
	// transcript trust) — it is a step gate now, not the checkmate.
	rd, issues := playbook.ParseBytes("recipe-derivation.md", []byte(mustTemplate("templates/recipe-derivation.md")))
	if len(issues) != 0 {
		t.Fatalf("recipe-derivation issues = %#v", issues)
	}
	if rd.Goal != nil {
		t.Fatalf("recipe-derivation should have no early [SetGoal] (it must run every reconcile step to END); goal = %#v", rd.Goal)
	}
	// tests-enumerated is still the fact: _Test predicate; perf-mcp is proven on its real
	// function (perf.scan_c, an mcp predicate) and gdb on real debugging (a shell gate) — not
	// gdb_diagnostics/toolchain_probe version checks.
	if te := rd.Verify["tests-enumerated"]; te.Kind != "fact" || te.Query != "_Test" {
		t.Fatalf("tests-enumerated verify = %#v", te)
	}
	if rd.Verify["perf-static-scan"].Kind != "mcp" {
		t.Fatalf("perf-static-scan should be an mcp predicate; got %#v", rd.Verify["perf-static-scan"])
	}
	if rd.Verify["perf-command-measured"].Kind != "mcp" {
		t.Fatalf("perf-command-measured should be an mcp predicate; got %#v", rd.Verify["perf-command-measured"])
	}
	if rd.Verify["gdb-debugs-real-binary"].Kind != "shell" {
		t.Fatalf("gdb-debugs-real-binary should be a shell predicate; got %#v", rd.Verify["gdb-debugs-real-binary"])
	}
	// Every gated step pins its predicate via [Submit]; the confirm step is a human [Checkpoint].
	// derive proves the cc-interposed build PUBLISHES facts (build-published, run under a no-match
	// filter so it needs no test env; it asserts only facts.published, not overall); prove then
	// proves the suite RUNS (candidate-proven, with the runtime environment discovered). enumerate
	// (tests-enumerated) is the facts-published proof —
	// there is no separate re-running "publish" step (a second src_compile run would be incremental
	// and never republish: it fails facts.published forever).
	if _, ok := rd.Steps["publish"]; ok {
		t.Fatalf("recipe-derivation should not have a separate publish step (derive publishes; enumerate proves it)")
	}
	for step, want := range map[string]string{
		"derive":         "build-published",
		"prove":          "candidate-proven",
		"enumerate":      "tests-enumerated",
		"reconcile-perf": "perf-static-scan",
		"reconcile-diag": "gdb-debugs-real-binary",
	} {
		if got := rd.Steps[step].Submit; got != want {
			t.Fatalf("recipe-derivation step %q Submit = %q, want %q", step, got, want)
		}
	}
	if rd.Steps["confirm"].Checkpoint == "" {
		t.Fatalf("recipe-derivation confirm step should be a [Checkpoint] human gate")
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
		// Each wired surface is proven by name on its REAL function (not a version probe):
		// the recipe build, the perf static analyzer, a real gdb session, and the human gate.
		"candidate-proven",
		"arbiter cc",
		"perf.scan_c",
		"gdb-mcp session",
		"SubmitCheckpoint",
		"facts snapshot",
		"__SANITIZE_ADDRESS__",
		"__has_feature",
		"facts.key_flags",
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
