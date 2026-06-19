package verify

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
)

func mustRaw(t *testing.T, s string) json.RawMessage {
	t.Helper()
	if !json.Valid([]byte(s)) {
		t.Fatalf("invalid fixture JSON: %s", s)
	}
	return json.RawMessage(s)
}

func TestDecodeSpecRejectsUnknownKeys(t *testing.T) {
	_, err := DecodeSpec(mustRaw(t, `{"kind":"run","tests":["t"],"expect":{"overall":"passed"},"bogus":1}`))
	if code := specCode(err); code != playbook.CodeBadResult {
		t.Fatalf("unknown key: code = %q, want %q (err=%v)", code, playbook.CodeBadResult, err)
	}
}

func TestDecodeSpecRoundTrips(t *testing.T) {
	spec, err := DecodeSpec(mustRaw(t, `{"kind":"fact","query":"sym:Foo","expect":{"min_results":1}}`))
	if err != nil {
		t.Fatal(err)
	}
	if spec.Kind != "fact" || spec.Query != "sym:Foo" {
		t.Fatalf("decoded spec = %+v", spec)
	}
}

func TestValidateClosedSets(t *testing.T) {
	cases := []struct {
		name string
		spec ResultSpec
	}{
		{"run missing recipe", ResultSpec{Kind: "run", Tests: []string{"t"}, Expect: mustRaw(t, `{"overall":"passed"}`)}},
		{"run blank recipe", ResultSpec{Kind: "run", Recipe: "  ", Tests: []string{"t"}, Expect: mustRaw(t, `{"overall":"passed"}`)}},
		{"run missing tests", ResultSpec{Kind: "run", Recipe: "unit", Expect: mustRaw(t, `{"overall":"passed"}`)}},
		{"run missing expect", ResultSpec{Kind: "run", Recipe: "unit", Tests: []string{"t"}}},
		{"run with shell command", ResultSpec{Kind: "run", Recipe: "unit", Command: "true", Tests: []string{"t"}, Expect: mustRaw(t, `{"overall":"passed"}`)}},
		{"run with mcp server", ResultSpec{Kind: "run", Recipe: "unit", Server: "x", Tests: []string{"t"}, Expect: mustRaw(t, `{"overall":"passed"}`)}},
		{"run unknown expect key", ResultSpec{Kind: "run", Recipe: "unit", Tests: []string{"t"}, Expect: mustRaw(t, `{"overall":"passed","junk":1}`)}},
		{"run overall wrong type", ResultSpec{Kind: "run", Recipe: "unit", Tests: []string{"t"}, Expect: mustRaw(t, `{"overall":7}`)}},
		{"run one_of empty", ResultSpec{Kind: "run", Recipe: "unit", Tests: []string{"t"}, Expect: mustRaw(t, `{"overall":{"one_of":[]}}`)}},
		{"run expect empty", ResultSpec{Kind: "run", Recipe: "unit", Tests: []string{"t"}, Expect: mustRaw(t, `{}`)}},
		{"run test clause incomplete", ResultSpec{Kind: "run", Recipe: "unit", Tests: []string{"t"}, Expect: mustRaw(t, `{"test":{"name":"x"}}`)}},
		{"run facts clause empty", ResultSpec{Kind: "run", Recipe: "unit", Tests: []string{"t"}, Expect: mustRaw(t, `{"facts":{}}`)}},
		{"run facts clause unknown key", ResultSpec{Kind: "run", Recipe: "unit", Tests: []string{"t"}, Expect: mustRaw(t, `{"facts":{"published":true,"junk":1}}`)}},
		{"fact missing query", ResultSpec{Kind: "fact", Expect: mustRaw(t, `{"min_results":1}`)}},
		{"fact missing expect", ResultSpec{Kind: "fact", Query: "sym:Foo"}},
		{"fact with tool", ResultSpec{Kind: "fact", Tool: "x", Query: "sym:Foo", Expect: mustRaw(t, `{"min_results":1}`)}},
		{"fact unknown expect key", ResultSpec{Kind: "fact", Query: "q", Expect: mustRaw(t, `{"junk":true}`)}},
		{"fact expect empty", ResultSpec{Kind: "fact", Query: "q", Expect: mustRaw(t, `{}`)}},
		{"fact negative min", ResultSpec{Kind: "fact", Query: "q", Expect: mustRaw(t, `{"min_results":-1}`)}},
		{"verify reference with inline kind", ResultSpec{Verify: "suite-green", Kind: "shell", Command: "true"}},
		{"verify reference with inline run", ResultSpec{Verify: "suite-green", Kind: "run", Recipe: "unit", Tests: []string{"t"}, Expect: mustRaw(t, `{"overall":"passed"}`)}},
		{"shell with recipe", ResultSpec{Kind: "shell", Command: "true", Recipe: "r"}},
		{"shell with query", ResultSpec{Kind: "shell", Command: "true", Query: "q"}},
		{"mcp with tests", ResultSpec{Kind: "mcp", Server: "s", Tool: "t", Tests: []string{"x"}}},
		{"shell with expect", ResultSpec{Kind: "shell", Command: "true", Expect: mustRaw(t, `[{"path":"x","op":"exists"}]`)}},
		{"mcp expect not array", ResultSpec{Kind: "mcp", Server: "s", Tool: "t", Expect: mustRaw(t, `{"path":"x","op":"exists"}`)}},
		{"mcp expect too many", ResultSpec{Kind: "mcp", Server: "s", Tool: "t", Expect: mustRaw(t, `[{"path":"a","op":"exists"},{"path":"b","op":"exists"},{"path":"c","op":"exists"},{"path":"d","op":"exists"},{"path":"e","op":"exists"},{"path":"f","op":"exists"},{"path":"g","op":"exists"},{"path":"h","op":"exists"},{"path":"i","op":"exists"}]`)}},
		{"mcp expect unknown op", ResultSpec{Kind: "mcp", Server: "s", Tool: "t", Expect: mustRaw(t, `[{"path":"x","op":"contains","value":"ok"}]`)}},
		{"mcp expect wildcard path", ResultSpec{Kind: "mcp", Server: "s", Tool: "t", Expect: mustRaw(t, `[{"path":"content.*.text","op":"exists"}]`)}},
		{"mcp expect object value", ResultSpec{Kind: "mcp", Server: "s", Tool: "t", Expect: mustRaw(t, `[{"path":"x","op":"eq","value":{"bad":true}}]`)}},
		{"mcp expect missing value", ResultSpec{Kind: "mcp", Server: "s", Tool: "t", Expect: mustRaw(t, `[{"path":"x","op":"eq"}]`)}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if code := specCode(Validate(tc.spec)); code != playbook.CodeBadResult {
				t.Fatalf("code = %q, want %q", code, playbook.CodeBadResult)
			}
		})
	}
}

func TestValidateAcceptsMCPExpectClauses(t *testing.T) {
	spec := ResultSpec{
		Kind:   "mcp",
		Server: "foreign",
		Tool:   "probe",
		Expect: mustRaw(t, `[
			{"path":"isError","op":"eq","value":false},
			{"path":"content.0.text","op":"exists"},
			{"path":"count","op":"ge","value":1}
		]`),
	}
	if err := Validate(spec); err != nil {
		t.Fatalf("unexpected validation error: %v", err)
	}
}

func TestMCPExpectClausesDriveVerdictAndReport(t *testing.T) {
	root := t.TempDir()
	stub := copiedSelf(t)
	writeMCP(t, root, map[string]any{
		"structured": map[string]any{
			"type":    "stdio",
			"command": stub,
			"env":     map[string]any{"ARBITER_TEST_STUB": "1", "ARBITER_TEST_MODE": "structured"},
		},
	})

	// expect 路径以 structuredContent 为根(ADR-0006/0010):信封字段
	// (isError、content)不可寻址;isError 由 runTool 自动门控。
	pass, err := Execute(context.Background(), root, ResultSpec{
		Kind:   "mcp",
		Server: "structured",
		Tool:   "probe",
		Expect: mustRaw(t, `[
			{"path":"ok","op":"eq","value":true},
			{"path":"state","op":"eq","value":"stopped"}
		]`),
	})
	if err != nil {
		t.Fatal(err)
	}
	if !Pass(pass) || pass.Verdict == nil || len(pass.ExpectReport) != 2 {
		t.Fatalf("pass result = %#v", pass)
	}

	fail, err := Execute(context.Background(), root, ResultSpec{
		Kind:   "mcp",
		Server: "structured",
		Tool:   "probe",
		Expect: mustRaw(t, `[{"path":"state","op":"eq","value":"nope"}]`),
	})
	if err != nil {
		t.Fatal(err)
	}
	if Pass(fail) || fail.Verdict == nil || *fail.Verdict {
		t.Fatalf("fail result = %#v", fail)
	}
	if len(fail.ExpectReport) != 1 || fail.ExpectReport[0].OK {
		t.Fatalf("fail report = %#v", fail.ExpectReport)
	}

	// 信封路径在新约定下不可解析 → fail-closed。
	envelope, err := Execute(context.Background(), root, ResultSpec{
		Kind:   "mcp",
		Server: "structured",
		Tool:   "probe",
		Expect: mustRaw(t, `[{"path":"isError","op":"eq","value":false}]`),
	})
	if err != nil {
		t.Fatal(err)
	}
	if Pass(envelope) {
		t.Fatalf("envelope path must not resolve: %#v", envelope.ExpectReport)
	}
}

func TestCompareMCPExpectOpsFailClosed(t *testing.T) {
	payload := map[string]any{
		"n": float64(3),
		"s": "ok",
		"b": true,
	}
	pass, err := ParseMCPExpect(mustRaw(t, `[
		{"path":"n","op":"ge","value":2},
		{"path":"n","op":"le","value":4},
		{"path":"s","op":"ne","value":"bad"},
		{"path":"b","op":"exists"}
	]`))
	if err != nil {
		t.Fatal(err)
	}
	if ok, report := CompareMCP(pass, payload); !ok {
		t.Fatalf("expected pass: %#v", report)
	}

	failures := []string{
		`[{"path":"missing","op":"exists"}]`,
		`[{"path":"missing","op":"eq","value":1}]`,
		`[{"path":"s","op":"ge","value":1}]`,
		`[{"path":"n","op":"ne","value":"3"}]`,
	}
	for _, raw := range failures {
		expect, err := ParseMCPExpect(mustRaw(t, raw))
		if err != nil {
			t.Fatal(err)
		}
		if ok, report := CompareMCP(expect, payload); ok {
			t.Fatalf("%s unexpectedly passed: %#v", raw, report)
		}
	}
}

func TestValidateAcceptsWellFormedTypedSpecs(t *testing.T) {
	good := []ResultSpec{
		{Kind: "run", Recipe: "unit", Tests: []string{"suite.case"}, Expect: mustRaw(t, `{"overall":"passed"}`)},
		{Kind: "run", Recipe: "unit", Tests: []string{"a", "b"}, Options: map[string]any{"profile": "fast"}, Expect: mustRaw(t, `{"overall":{"one_of":["passed","flaky"]},"max_failed":0,"min_passed":2,"test":{"name":"a","result":"passed"},"facts":{"published":true}}`)},
		{Kind: "fact", Query: "sym:Router", Expect: mustRaw(t, `{"min_results":1,"max_results":10,"complete":true,"reachable":true,"total_at_least":1}`)},
	}
	for i, spec := range good {
		if err := Validate(spec); err != nil {
			t.Fatalf("spec[%d]: unexpected error %v", i, err)
		}
	}
}

func TestCompareRunVerdictAndReport(t *testing.T) {
	expect, err := ParseRunExpect(mustRaw(t, `{"overall":"passed","max_failed":0,"min_passed":2,"test":{"name":"a","result":"passed"}}`))
	if err != nil {
		t.Fatal(err)
	}
	ev := RunEvidence{RunID: "r1", Overall: "passed", Passed: 3, Failed: 0, TestResults: map[string]string{"a": "passed"}}
	ok, report := CompareRun(expect, ev)
	if !ok {
		t.Fatalf("verdict = false, want true; report=%+v", report)
	}
	if len(report) != 4 {
		t.Fatalf("report len = %d, want 4", len(report))
	}
	paths := map[string]bool{}
	for _, clause := range report {
		if clause.Path == "" || clause.Op == "" {
			t.Fatalf("clause missing path/op: %+v", clause)
		}
		if !clause.OK {
			t.Fatalf("clause not ok: %+v", clause)
		}
		paths[clause.Path] = true
	}
	for _, want := range []string{"overall", "max_failed", "min_passed", "test.a"} {
		if !paths[want] {
			t.Fatalf("missing clause path %q in %v", want, paths)
		}
	}

	bad := RunEvidence{RunID: "r2", Overall: "failed", Passed: 1, Failed: 2, TestResults: map[string]string{"a": "failed"}}
	ok, report = CompareRun(expect, bad)
	if ok {
		t.Fatal("verdict = true, want false")
	}
	failing := 0
	for _, clause := range report {
		if !clause.OK {
			failing++
			if clause.Actual == nil {
				t.Fatalf("failing clause carries no actual: %+v", clause)
			}
		}
	}
	if failing != 4 {
		t.Fatalf("failing clauses = %d, want 4", failing)
	}
}

func TestCompareRunOneOf(t *testing.T) {
	expect, err := ParseRunExpect(mustRaw(t, `{"overall":{"one_of":["passed","flaky"]}}`))
	if err != nil {
		t.Fatal(err)
	}
	if ok, _ := CompareRun(expect, RunEvidence{Overall: "flaky"}); !ok {
		t.Fatal("one_of member rejected")
	}
	if ok, _ := CompareRun(expect, RunEvidence{Overall: "failed"}); ok {
		t.Fatal("one_of non-member accepted")
	}
}

// An "errored" run (build broke, no result file, timed out before completion -
// the engine could obtain no test verdict) must satisfy NEITHER a red gate
// (expect overall=failed) NOR a green gate (expect overall=passed). This is the
// referee-side half of the engine contract that a non-compiling test does not
// count as a reproduced failure: the gtest adapter emits overall="errored" for
// build/harness failures, and exact-set matching here rejects it both ways.
func TestCompareRunErroredSatisfiesNoGate(t *testing.T) {
	redGate, err := ParseRunExpect(mustRaw(t, `{"overall":"failed"}`))
	if err != nil {
		t.Fatal(err)
	}
	if ok, report := CompareRun(redGate, RunEvidence{Overall: "errored"}); ok {
		t.Fatalf("build error satisfied a run-red gate: %#v", report)
	}
	greenGate, err := ParseRunExpect(mustRaw(t, `{"overall":"passed","max_failed":0}`))
	if err != nil {
		t.Fatal(err)
	}
	if ok, report := CompareRun(greenGate, RunEvidence{Overall: "errored"}); ok {
		t.Fatalf("build error satisfied a suite-green gate: %#v", report)
	}
	// A genuine assertion failure (the test ran and went red) still satisfies the
	// red gate - errored is strictly narrower than failed, not a rename of it.
	if ok, _ := CompareRun(redGate, RunEvidence{Overall: "failed", Failed: 1}); !ok {
		t.Fatal("a real assertion failure was rejected by the run-red gate")
	}
}

func TestCompareRunFactsPublished(t *testing.T) {
	expect, err := ParseRunExpect(mustRaw(t, `{"overall":"passed","facts":{"published":true}}`))
	if err != nil {
		t.Fatal(err)
	}
	ok, report := CompareRun(expect, RunEvidence{Overall: "passed", Facts: &RunFactsEvidence{Published: true}})
	if !ok {
		t.Fatalf("facts clause did not pass: %#v", report)
	}
	missingOK, missingReport := CompareRun(expect, RunEvidence{Overall: "passed"})
	if missingOK {
		t.Fatalf("missing facts unexpectedly passed: %#v", missingReport)
	}
	var found bool
	for _, clause := range missingReport {
		if clause.Path == "facts.published" {
			found = true
			if clause.OK || clause.Actual != nil {
				t.Fatalf("missing facts clause = %#v", clause)
			}
		}
	}
	if !found {
		t.Fatalf("missing facts.published report: %#v", missingReport)
	}
}

// A facts-ONLY run expect (no "overall" clause) is satisfied by a run whose build
// published the index, even when the run's overall verdict is "errored". This is
// the contract behind the recipe-derivation `build-published` gate: facts publish
// from the src_compile stage BEFORE test_run, so a deliberate no-match test filter
// (overall=errored, no_tests_ran) still proves the build + index — without claiming
// any test passed. CompareRun must check only the clauses present, never an implicit
// overall gate.
func TestCompareRunFactsOnlyIgnoresErroredOverall(t *testing.T) {
	expect, err := ParseRunExpect(mustRaw(t, `{"facts":{"published":true}}`))
	if err != nil {
		t.Fatal(err)
	}
	// Build published the index but no test matched the filter -> overall errored.
	if ok, report := CompareRun(expect, RunEvidence{Overall: "errored", Facts: &RunFactsEvidence{Published: true}}); !ok {
		t.Fatalf("facts-only gate rejected an errored run that published facts: %#v", report)
	}
	// Build failed / index did not publish -> the gate must NOT pass.
	if ok, _ := CompareRun(expect, RunEvidence{Overall: "errored"}); ok {
		t.Fatal("facts-only gate passed with no facts published")
	}
	if ok, _ := CompareRun(expect, RunEvidence{Overall: "errored", Facts: &RunFactsEvidence{Published: false}}); ok {
		t.Fatal("facts-only gate passed with facts.published=false")
	}
}

func TestCompareFactClauses(t *testing.T) {
	expect, err := ParseFactExpect(mustRaw(t, `{"min_results":1,"max_results":3,"complete":true,"reachable":true,"total_at_least":2}`))
	if err != nil {
		t.Fatal(err)
	}
	ev := FactEvidence{SnapshotID: "s1", ResultCount: 2, Complete: true, Reachable: true, TotalResults: 5}
	ok, report := CompareFact(expect, ev)
	if !ok {
		t.Fatalf("verdict = false, want true; report=%+v", report)
	}
	if len(report) != 5 {
		t.Fatalf("report len = %d, want 5", len(report))
	}
	ev.ResultCount = 0
	ev.Complete = false
	if ok, _ = CompareFact(expect, ev); ok {
		t.Fatal("verdict = true, want false")
	}
}

func TestExecuteFactPredicateUsesEngineSearchAndRefresh(t *testing.T) {
	root := verifyRepoRoot(t)
	result, err := Execute(context.Background(), root, ResultSpec{
		Kind:   "fact",
		Query:  "alpha",
		Expect: mustRaw(t, `{"max_results":0,"complete":true}`),
	})
	if err != nil {
		t.Fatal(err)
	}
	if !Pass(result) || result.Verdict == nil || !*result.Verdict {
		t.Fatalf("fact predicate did not pass: %#v", result)
	}
	var evidence FactEvidence
	if err := json.Unmarshal(result.Evidence, &evidence); err != nil {
		t.Fatal(err)
	}
	// The source repo has no published facts snapshot, so refresh reconciles to a clean base
	// view (an overlay is published only when sources are dirty vs a snapshot, ADR-0018).
	if evidence.ViewState != "base" || evidence.OverlayID != "" {
		t.Fatalf("expected clean base view from refresh: %#v", evidence)
	}
	if evidence.ResultCount != 0 || !evidence.Complete {
		t.Fatalf("evidence counters = %#v", evidence)
	}
	if len(result.ExpectReport) != 2 {
		t.Fatalf("expect report = %#v", result.ExpectReport)
	}
}

// run 谓词只有在引擎子进程根本无法 spawn 时才报 engine_unavailable
// (其余引擎故障以 result.Failure fail-closed,不再有 #37/#43 占位分支)。
func TestExecuteTypedRunFailsClosedWithoutEngine(t *testing.T) {
	t.Setenv("PYTHON", filepath.Join(t.TempDir(), "missing-python"))
	spec := ResultSpec{
		Kind:   "run",
		Recipe: "unit",
		Tests:  []string{"t"},
		Expect: mustRaw(t, `{"overall":"passed"}`),
	}
	_, err := Execute(context.Background(), t.TempDir(), spec)
	if code := specCode(err); code != playbook.CodeEngineUnavailable {
		t.Fatalf("code = %q, want %q (err=%v)", code, playbook.CodeEngineUnavailable, err)
	}
	if err != nil && !strings.Contains(err.Error(), "engine") {
		t.Fatalf("error should mention engine: %v", err)
	}
}

// 快乐路径:经真实引擎的 run 工具执行假 gtest recipe,产出判定/证据/逐条对照。
func TestExecuteRunPredicateUsesEngineRunTool(t *testing.T) {
	root := t.TempDir()
	t.Setenv("PYTHONPATH", filepath.Join(verifyRepoRoot(t), "engine"))
	writeRunFixture(t, root)

	result, err := Execute(context.Background(), root, ResultSpec{
		Kind:   "run",
		Recipe: "unit",
		Tests:  []string{"Suite.Pass"},
		Expect: mustRaw(t, `{"overall":"passed","min_passed":1,"test":{"name":"Suite.Pass","result":"passed"}}`),
	})
	if err != nil {
		t.Fatal(err)
	}
	if !Pass(result) || result.Verdict == nil || !*result.Verdict {
		t.Fatalf("run predicate did not pass: %#v", result)
	}
	var evidence RunEvidence
	if err := json.Unmarshal(result.Evidence, &evidence); err != nil {
		t.Fatal(err)
	}
	if evidence.Overall != "passed" || evidence.Passed != 1 || evidence.TestResults["Suite.Pass"] != "passed" {
		t.Fatalf("evidence = %#v", evidence)
	}
	if len(result.ExpectReport) != 3 {
		t.Fatalf("expect report = %#v", result.ExpectReport)
	}
}

// writeRunFixture 写一份引擎可执行的 v2 recipes.yaml 与假 gtest 脚本。
func writeRunFixture(t *testing.T, root string) {
	t.Helper()
	script := filepath.Join(root, "fake_gtest.sh")
	body := "#!/bin/sh\n" +
		"for arg in \"$@\"; do\n" +
		"  case \"$arg\" in --gtest_output=xml:*) out=\"${arg#--gtest_output=xml:}\" ;; esac\n" +
		"done\n" +
		"mkdir -p \"$(dirname \"$out\")\"\n" +
		"printf '%s\\n' '<testsuites tests=\"1\" failures=\"0\"><testsuite name=\"Suite\"><testcase classname=\"Suite\" name=\"Pass\" time=\"0.001\"/></testsuite></testsuites>' > \"$out\"\n" +
		"exit 0\n"
	if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	recipes := "targets:\n" +
		"  - id: unit\n" +
		"    harness:\n" +
		"      kind: gtest\n" +
		"    test_run:\n" +
		"      cmd: [" + script + "]\n"
	if err := os.MkdirAll(filepath.Join(root, ".arbiter"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, ".arbiter", "recipes.yaml"), []byte(recipes), 0o644); err != nil {
		t.Fatal(err)
	}
}

// facts.published 的双极性:证据缺 facts 节 ⇒ 未发布。
func TestCompareRunFactsPublishedAbsentFacts(t *testing.T) {
	wantAbsent, err := ParseRunExpect(mustRaw(t, `{"facts":{"published":false}}`))
	if err != nil {
		t.Fatal(err)
	}
	ok, report := CompareRun(wantAbsent, RunEvidence{Overall: "passed"})
	if !ok {
		t.Fatalf("published:false must pass on absent facts: %#v", report)
	}
	if len(report) != 1 || report[0].Actual != nil {
		t.Fatalf("actual must be recorded as absent: %#v", report)
	}

	wantPublished, err := ParseRunExpect(mustRaw(t, `{"facts":{"published":true}}`))
	if err != nil {
		t.Fatal(err)
	}
	ok, report = CompareRun(wantPublished, RunEvidence{Overall: "passed"})
	if ok {
		t.Fatalf("published:true must fail on absent facts: %#v", report)
	}
	if len(report) != 1 || report[0].Actual != nil {
		t.Fatalf("actual must be recorded as absent: %#v", report)
	}
}

func TestRunPerTestHelpers(t *testing.T) {
	perTest := []RunPerTest{
		{Suite: "Suite", Name: "Pass", Status: "passed"},
		{Suite: "Suite", Name: "Skip", Status: "skipped"},
		{Suite: "Suite", Name: "Boom", Status: "failed"},
		{Name: "Loose", Status: "failed"},
	}
	results := RunTestResults(perTest)
	if results["Suite.Pass"] != "passed" || results["Suite.Boom"] != "failed" || results["Loose"] != "failed" {
		t.Fatalf("results = %#v", results)
	}
	if first := FirstRunFailure(perTest); first != "Suite.Boom" {
		t.Fatalf("first failure = %q", first)
	}
	if RunTestResults(nil) != nil {
		t.Fatal("empty per_test should produce nil map")
	}
	if FirstRunFailure(nil) != "" {
		t.Fatal("no failures should yield empty name")
	}
}

func TestRefreshDedupeRecordsOnlySuccessAndLatestRound(t *testing.T) {
	root := filepath.Join(t.TempDir(), "repo")
	meta1 := map[string]any{"match_id": "m1", "round_seq": 1}
	if !shouldRefreshFacts(root, meta1) {
		t.Fatal("first sighting must refresh")
	}
	// 失败路径不记录:再次询问仍要求 refresh
	if !shouldRefreshFacts(root, meta1) {
		t.Fatal("unrecorded round must still refresh")
	}
	recordFactsRefreshed(root, meta1)
	if shouldRefreshFacts(root, meta1) {
		t.Fatal("recorded round must dedupe")
	}
	meta2 := map[string]any{"match_id": "m1", "round_seq": 2}
	if !shouldRefreshFacts(root, meta2) {
		t.Fatal("new round must refresh")
	}
	recordFactsRefreshed(root, meta2)
	if shouldRefreshFacts(root, meta2) {
		t.Fatal("latest round must dedupe")
	}
	// 同一 root+match 只保留最新回合的键,不随回合数增长
	refreshDedupe.Lock()
	defer refreshDedupe.Unlock()
	count := 0
	for key := range refreshDedupe.seen {
		if strings.HasPrefix(key, root+"\x00m1") {
			count++
		}
	}
	if count != 1 {
		t.Fatalf("dedupe entries for match = %d, want 1", count)
	}
}

func verifyRepoRoot(t *testing.T) string {
	t.Helper()
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("runtime.Caller failed")
	}
	return filepath.Clean(filepath.Join(filepath.Dir(file), "..", ".."))
}

func TestPassConsultsTypedVerdictFirst(t *testing.T) {
	yes, no := true, false
	zero := 0
	if !Pass(Result{Verdict: &yes}) {
		t.Fatal("verdict true should pass")
	}
	if Pass(Result{Verdict: &no, ExitCode: &zero}) {
		t.Fatal("verdict false must fail even with exit 0")
	}
	if !Pass(Result{ExitCode: &zero}) {
		t.Fatal("shell semantics changed: exit 0 must still pass")
	}
	if Pass(Result{}) {
		t.Fatal("no signal must stay fail-closed")
	}
	// A typed run whose expect verdict PASSED but which carries a diagnostic
	// failure code (e.g. no_tests_ran on a facts-only build-published gate, where
	// the no-match filter makes the run overall=errored while facts still publish)
	// must PASS: the verdict is the only signal, the failure code is audit-only.
	// Regression for the e2e bug where Pass() failed build-published on no_tests_ran.
	if !Pass(Result{Verdict: &yes, Failure: "no_tests_ran"}) {
		t.Fatal("typed verdict true must pass despite a diagnostic run failure code")
	}
	if Pass(Result{Verdict: &no, Failure: "no_tests_ran"}) {
		t.Fatal("typed verdict false must still fail")
	}
	// Infrastructure failures leave Verdict nil; those stay fail-closed on Failure.
	if Pass(Result{Failure: "timeout"}) {
		t.Fatal("an infra failure with no verdict must fail-closed")
	}
}

func specCode(err error) string {
	var spec *SpecError
	if err == nil {
		return ""
	}
	if errorsAs(err, &spec) {
		return spec.Code
	}
	return ""
}

func errorsAs(err error, target **SpecError) bool {
	for err != nil {
		if s, ok := err.(*SpecError); ok {
			*target = s
			return true
		}
		type unwrapper interface{ Unwrap() error }
		u, ok := err.(unwrapper)
		if !ok {
			return false
		}
		err = u.Unwrap()
	}
	return false
}
