package verify

import (
	"context"
	"encoding/json"
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
		{"run missing tests", ResultSpec{Kind: "run", Expect: mustRaw(t, `{"overall":"passed"}`)}},
		{"run missing expect", ResultSpec{Kind: "run", Tests: []string{"t"}}},
		{"run with shell command", ResultSpec{Kind: "run", Command: "true", Tests: []string{"t"}, Expect: mustRaw(t, `{"overall":"passed"}`)}},
		{"run with mcp server", ResultSpec{Kind: "run", Server: "x", Tests: []string{"t"}, Expect: mustRaw(t, `{"overall":"passed"}`)}},
		{"run unknown expect key", ResultSpec{Kind: "run", Tests: []string{"t"}, Expect: mustRaw(t, `{"overall":"passed","junk":1}`)}},
		{"run overall wrong type", ResultSpec{Kind: "run", Tests: []string{"t"}, Expect: mustRaw(t, `{"overall":7}`)}},
		{"run one_of empty", ResultSpec{Kind: "run", Tests: []string{"t"}, Expect: mustRaw(t, `{"overall":{"one_of":[]}}`)}},
		{"run expect empty", ResultSpec{Kind: "run", Tests: []string{"t"}, Expect: mustRaw(t, `{}`)}},
		{"run test clause incomplete", ResultSpec{Kind: "run", Tests: []string{"t"}, Expect: mustRaw(t, `{"test":{"name":"x"}}`)}},
		{"fact missing query", ResultSpec{Kind: "fact", Expect: mustRaw(t, `{"min_results":1}`)}},
		{"fact missing expect", ResultSpec{Kind: "fact", Query: "sym:Foo"}},
		{"fact with tool", ResultSpec{Kind: "fact", Tool: "x", Query: "sym:Foo", Expect: mustRaw(t, `{"min_results":1}`)}},
		{"fact unknown expect key", ResultSpec{Kind: "fact", Query: "q", Expect: mustRaw(t, `{"junk":true}`)}},
		{"fact expect empty", ResultSpec{Kind: "fact", Query: "q", Expect: mustRaw(t, `{}`)}},
		{"fact negative min", ResultSpec{Kind: "fact", Query: "q", Expect: mustRaw(t, `{"min_results":-1}`)}},
		{"shell with recipe", ResultSpec{Kind: "shell", Command: "true", Recipe: "r"}},
		{"shell with query", ResultSpec{Kind: "shell", Command: "true", Query: "q"}},
		{"mcp with tests", ResultSpec{Kind: "mcp", Server: "s", Tool: "t", Tests: []string{"x"}}},
		{"mcp with expect", ResultSpec{Kind: "mcp", Server: "s", Tool: "t", Expect: mustRaw(t, `{"min_results":1}`)}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if code := specCode(Validate(tc.spec)); code != playbook.CodeBadResult {
				t.Fatalf("code = %q, want %q", code, playbook.CodeBadResult)
			}
		})
	}
}

func TestValidateAcceptsWellFormedTypedSpecs(t *testing.T) {
	good := []ResultSpec{
		{Kind: "run", Tests: []string{"suite.case"}, Expect: mustRaw(t, `{"overall":"passed"}`)},
		{Kind: "run", Recipe: "unit", Tests: []string{"a", "b"}, Options: map[string]any{"profile": "fast"}, Expect: mustRaw(t, `{"overall":{"one_of":["passed","flaky"]},"max_failed":0,"min_passed":2,"test":{"name":"a","result":"passed"}}`)},
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

func TestExecuteTypedKindsFailClosedWithoutEngine(t *testing.T) {
	for _, kind := range []string{"run", "fact"} {
		spec := ResultSpec{Kind: kind}
		if kind == "run" {
			spec.Tests = []string{"t"}
			spec.Expect = mustRaw(t, `{"overall":"passed"}`)
		} else {
			spec.Query = "sym:Foo"
			spec.Expect = mustRaw(t, `{"min_results":1}`)
		}
		_, err := Execute(context.Background(), t.TempDir(), spec)
		if code := specCode(err); code != playbook.CodeEngineUnavailable {
			t.Fatalf("%s: code = %q, want %q (err=%v)", kind, code, playbook.CodeEngineUnavailable, err)
		}
		if err != nil && !strings.Contains(err.Error(), "engine") {
			t.Fatalf("%s: error should mention engine: %v", kind, err)
		}
	}
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
