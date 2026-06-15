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
			verify:     []string{"gear-up-published", "candidate-proven", "tests-enumerated"},
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

	// recipe-derivation 的 checkmate 现在是 tests-enumerated —— 一个 fact: TestBody 谓词,
	// 裁判对已发布快照自行重跑 TestBody 索引查询。它比旧的 gear-up-published goal 更严:
	// TestBody facts 只可能存在于已发布的快照,所以它传递性地保留了"facts 必须真发布"的
	// 反作弊保证(弱内联谓词蒙混不过裁判自查的快照),并额外强制项目的完整测试集由索引
	// 枚举得出、而非取信于模型转录(scan 走的也是 store.search("TestBody"))。走到 END 仍
	// 枚举不出完整且非空的测试集 ⇒ 败局。
	rd, issues := playbook.ParseBytes("recipe-derivation.md", []byte(mustTemplate("templates/recipe-derivation.md")))
	if len(issues) != 0 {
		t.Fatalf("recipe-derivation issues = %#v", issues)
	}
	if rd.Goal == nil || rd.Goal.Kind != "fact" || rd.Goal.Query != "TestBody" {
		t.Fatalf("recipe-derivation goal = %#v", rd.Goal)
	}
	if string(rd.Goal.Expect) != string(rd.Verify["tests-enumerated"].Expect) {
		t.Fatalf("goal expect %s != tests-enumerated expect %s", rd.Goal.Expect, rd.Verify["tests-enumerated"].Expect)
	}
	// 每个可裁决步骤都用 [Submit] 把谓词钉死,模型既不能自拟也不能改选。
	for step, want := range map[string]string{"derive": "candidate-proven", "publish": "gear-up-published"} {
		if got := rd.Steps[step].Submit; got != want {
			t.Fatalf("recipe-derivation step %q Submit = %q, want %q", step, got, want)
		}
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
