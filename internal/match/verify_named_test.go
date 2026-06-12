package match

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
	"github.com/HoldThatThgt/arbiter/internal/verify"
)

const namedVerifyBook = `---
name: namedbook
description: named predicate flow
verify_policy: named
---

[Verify] pass
shell: exit 0

[Verify] fail
shell: exit 3

[STEP] only
[StepJob]
do it
[CheckList]
- Submit pass
[Branch]
success: END
failure: only
`

const overridesBook = `---
name: overrides
description: override flow
---

[Verify] open-tests
run: unit
tests: ["Suite.A"]
expect: {"overall":"passed"}
allow_overrides: ["tests"]

[Verify] closed
run: unit
tests: ["Suite.A"]
expect: {"overall":"passed"}

[STEP] only
[StepJob]
do it
[CheckList]
- Submit open-tests
[Branch]
success: END
failure: only
`

func TestLoadPlayBookSnapshotsVerifyPolicyAndSpecs(t *testing.T) {
	root := repoWithBook(t, "named.md", namedVerifyBook)
	store := New(root, "test")
	if _, err := store.LoadPlayBook("namedbook"); err != nil {
		t.Fatal(err)
	}
	state := readStateFile(t, root)
	if state.VerifyPolicy != "named" {
		t.Fatalf("verify_policy = %q", state.VerifyPolicy)
	}
	if len(state.VerifySpecs) != 2 {
		t.Fatalf("verify_specs = %#v", state.VerifySpecs)
	}
	if spec := state.VerifySpecs["pass"]; spec.Kind != "shell" || spec.Command != "exit 0" {
		t.Fatalf("pass spec = %#v", spec)
	}
	if spec := state.VerifySpecs["fail"]; spec.Kind != "shell" || spec.Command != "exit 3" {
		t.Fatalf("fail spec = %#v", spec)
	}
}

func TestSubmitTaskNamedPredicate(t *testing.T) {
	root := repoWithBook(t, "named.md", namedVerifyBook)
	store := New(root, "test")
	if _, err := store.LoadPlayBook("namedbook"); err != nil {
		t.Fatal(err)
	}
	task, err := store.CreateTask("prove it")
	if err != nil {
		t.Fatal(err)
	}
	// 装载后改写棋谱文件:解析只对照对局快照,文件不再有发言权。
	path := filepath.Join(root, ".arbiter", "playbook", "named.md")
	if err := os.WriteFile(path, []byte(strings.ReplaceAll(namedVerifyBook, "exit 0", "exit 9")), 0o644); err != nil {
		t.Fatal(err)
	}
	submitted, err := store.SubmitTask(context.Background(), task.TaskID, "named pass", "r", verify.ResultSpec{Verify: "pass"})
	if err != nil {
		t.Fatal(err)
	}
	if submitted.Verdict != TaskPass {
		t.Fatalf("verdict = %q", submitted.Verdict)
	}
	review, err := store.ReviewTask(task.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	if review.Result == nil || review.Result.Spec.Kind != "shell" || review.Result.Spec.Command != "exit 0" {
		t.Fatalf("resolved spec = %#v", review.Result)
	}
	data, err := os.ReadFile(filepath.Join(root, ".arbiter", "match", "log", "journal.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(data), `"verify":"pass"`) {
		t.Fatalf("journal missing verify name: %s", data)
	}

	failTask, err := store.CreateTask("prove failure routing")
	if err != nil {
		t.Fatal(err)
	}
	failed, err := store.SubmitTask(context.Background(), failTask.TaskID, "named fail", "r", verify.ResultSpec{Verify: "fail"})
	if err != nil {
		t.Fatal(err)
	}
	if failed.Verdict != TaskFail {
		t.Fatalf("verdict = %q", failed.Verdict)
	}
}

func TestSubmitTaskVerifyNotFound(t *testing.T) {
	root := repoWithBook(t, "named.md", namedVerifyBook)
	store := New(root, "test")
	if _, err := store.LoadPlayBook("namedbook"); err != nil {
		t.Fatal(err)
	}
	task, err := store.CreateTask("t")
	if err != nil {
		t.Fatal(err)
	}
	_, err = store.SubmitTask(context.Background(), task.TaskID, "s", "r", verify.ResultSpec{Verify: "missing"})
	if code := toolCode(err); code != playbook.CodeVerifyNotFound {
		t.Fatalf("code = %q, want %q (err=%v)", code, playbook.CodeVerifyNotFound, err)
	}
}

func TestSubmitTaskNamedPolicyRejectsInline(t *testing.T) {
	root := repoWithBook(t, "named.md", namedVerifyBook)
	store := New(root, "test")
	if _, err := store.LoadPlayBook("namedbook"); err != nil {
		t.Fatal(err)
	}
	task, err := store.CreateTask("t")
	if err != nil {
		t.Fatal(err)
	}
	_, err = store.SubmitTask(context.Background(), task.TaskID, "s", "r", verify.ResultSpec{Kind: "shell", Command: "exit 0"})
	if code := toolCode(err); code != playbook.CodeVerifyPolicy {
		t.Fatalf("code = %q, want %q (err=%v)", code, playbook.CodeVerifyPolicy, err)
	}
}

func TestSubmitTaskVerifyMixedInlineRejected(t *testing.T) {
	root := repoWithBook(t, "named.md", namedVerifyBook)
	store := New(root, "test")
	if _, err := store.LoadPlayBook("namedbook"); err != nil {
		t.Fatal(err)
	}
	task, err := store.CreateTask("t")
	if err != nil {
		t.Fatal(err)
	}
	mixed := []verify.ResultSpec{
		{Verify: "pass", Kind: "shell", Command: "exit 0"},
		{Verify: "pass", Command: "exit 0"},
		{Verify: "pass", Expect: json.RawMessage(`{"min_results":1}`)},
		{Verify: "pass", TimeoutS: 30},
	}
	for _, spec := range mixed {
		if _, err := store.SubmitTask(context.Background(), task.TaskID, "s", "r", spec); toolCode(err) != playbook.CodeBadResult {
			t.Fatalf("spec %#v: err = %#v, want %s", spec, err, playbook.CodeBadResult)
		}
	}
}

func TestSubmitTaskVerifyOverrides(t *testing.T) {
	root := repoWithBook(t, "overrides.md", overridesBook)
	writeRecipes(t, root, "unit", "cmd: make test\n")
	store := New(root, "test")
	if _, err := store.LoadPlayBook("overrides"); err != nil {
		t.Fatal(err)
	}
	task, err := store.CreateTask("t")
	if err != nil {
		t.Fatal(err)
	}

	// 未声明的覆盖被拒:closed 没有 allow_overrides。
	_, err = store.SubmitTask(context.Background(), task.TaskID, "s", "r", verify.ResultSpec{Verify: "closed", Tests: []string{"Suite.B"}})
	if code := toolCode(err); code != playbook.CodeVerifyOverride {
		t.Fatalf("blocked tests override: code = %q, want %q (err=%v)", code, playbook.CodeVerifyOverride, err)
	}
	// open-tests 只放行 tests,options 覆盖仍被拒。
	_, err = store.SubmitTask(context.Background(), task.TaskID, "s", "r", verify.ResultSpec{Verify: "open-tests", Options: map[string]any{"profile": "fast"}})
	if code := toolCode(err); code != playbook.CodeVerifyOverride {
		t.Fatalf("blocked options override: code = %q, want %q (err=%v)", code, playbook.CodeVerifyOverride, err)
	}
	// allow_overrides 是棋谱侧声明,提交侧出现即拒绝。
	_, err = store.SubmitTask(context.Background(), task.TaskID, "s", "r", verify.ResultSpec{Verify: "open-tests", AllowOverrides: []string{"tests"}})
	if code := toolCode(err); code != playbook.CodeVerifyOverride {
		t.Fatalf("submission allow_overrides: code = %q, want %q (err=%v)", code, playbook.CodeVerifyOverride, err)
	}

	// 放行的 tests 覆盖通过解析与校验,推进到既有的 recipe pin 闸口:
	// 改写 recipes.yaml 制造 pin 失配,证明解析后的 run 谓词照常被 pin 检查拦下。
	writeRecipes(t, root, "unit", "cmd: make test CHANGED=1\n")
	_, err = store.SubmitTask(context.Background(), task.TaskID, "s", "r", verify.ResultSpec{Verify: "open-tests", Tests: []string{"Suite.B"}})
	if code := toolCode(err); code != playbook.CodeRecipePinMismatch {
		t.Fatalf("allowed override: code = %q, want %q (err=%v)", code, playbook.CodeRecipePinMismatch, err)
	}
}

func TestResolveVerifySpecMergesOverrides(t *testing.T) {
	curated := playbook.ResultSpec{
		Kind:           "run",
		Recipe:         "unit",
		Tests:          []string{"Suite.A"},
		Expect:         json.RawMessage(`{"overall":"passed"}`),
		AllowOverrides: []string{"tests", "options"},
	}
	m := &Match{VerifyPolicy: "named", VerifySpecs: map[string]verify.ResultSpec{"open": curated}}

	resolved, name, err := resolveVerifySpec(m, verify.ResultSpec{
		Verify:  "open",
		Tests:   []string{"Suite.B", "Suite.C"},
		Options: map[string]any{"profile": "fast"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if name != "open" {
		t.Fatalf("name = %q", name)
	}
	if resolved.Kind != "run" || resolved.Recipe != "unit" || string(resolved.Expect) != `{"overall":"passed"}` {
		t.Fatalf("resolved = %#v", resolved)
	}
	if strings.Join(resolved.Tests, ",") != "Suite.B,Suite.C" {
		t.Fatalf("resolved tests = %#v", resolved.Tests)
	}
	if resolved.Options["profile"] != "fast" {
		t.Fatalf("resolved options = %#v", resolved.Options)
	}
	if resolved.Verify != "" || len(resolved.AllowOverrides) != 0 {
		t.Fatalf("resolved spec still carries curator fields: %#v", resolved)
	}
	// 解析返回深拷贝:改写解析结果不能波及对局快照。
	resolved.Tests[0] = "mutated"
	if m.VerifySpecs["open"].Tests[0] != "Suite.A" {
		t.Fatalf("snapshot mutated: %#v", m.VerifySpecs["open"])
	}

	// 不带覆盖的引用沿用 curated 字段。
	plain, _, err := resolveVerifySpec(m, verify.ResultSpec{Verify: "open"})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(plain.Tests, ",") != "Suite.A" || plain.Options != nil {
		t.Fatalf("plain = %#v", plain)
	}
}

func TestShowStepJobListsVerifyDecls(t *testing.T) {
	root := repoWithBook(t, "named.md", namedVerifyBook)
	store := New(root, "test")
	if _, err := store.LoadPlayBook("namedbook"); err != nil {
		t.Fatal(err)
	}
	show, err := store.ShowStepJob()
	if err != nil {
		t.Fatal(err)
	}
	if len(show.Verify) != 2 {
		t.Fatalf("verify decls = %#v", show.Verify)
	}
	if show.Verify[0].Name != "fail" || show.Verify[1].Name != "pass" {
		t.Fatalf("verify decls not sorted: %#v", show.Verify)
	}
	for _, decl := range show.Verify {
		if decl.Kind != "shell" {
			t.Fatalf("decl = %#v", decl)
		}
	}
}
