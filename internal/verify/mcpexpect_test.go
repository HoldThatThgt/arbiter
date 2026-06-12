package verify

import (
	"testing"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
)

// mcp-kind expect[] 子句(ADR-0006 / go-referee.md#ResultSpec):
// 封闭操作集 eq|ne|ge|le|exists、标量值、≤8 条、点号路径,对照外部服务器
// structuredContent;路径缺失与类型不匹配一律 fail-closed。

func TestParseMCPExpectAccepts(t *testing.T) {
	cases := []struct {
		name string
		raw  string
		want int
	}{
		{"absent", ``, 0},
		{"single eq", `[{"path":"overall","op":"eq","value":"passed"}]`, 1},
		{"all ops", `[
			{"path":"a","op":"eq","value":1},
			{"path":"b","op":"ne","value":"x"},
			{"path":"c","op":"ge","value":0},
			{"path":"d","op":"le","value":9.5},
			{"path":"e","op":"exists"},
			{"path":"f.g","op":"eq","value":true}
		]`, 6},
		{"eight clauses", `[
			{"path":"p1","op":"exists"},{"path":"p2","op":"exists"},
			{"path":"p3","op":"exists"},{"path":"p4","op":"exists"},
			{"path":"p5","op":"exists"},{"path":"p6","op":"exists"},
			{"path":"p7","op":"exists"},{"path":"p8","op":"exists"}
		]`, 8},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var raw []byte
			if tc.raw != "" {
				raw = mustRaw(t, tc.raw)
			}
			clauses, err := ParseMCPExpect(raw)
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if len(clauses) != tc.want {
				t.Fatalf("clauses = %d, want %d", len(clauses), tc.want)
			}
		})
	}
}

func TestParseMCPExpectFailsClosed(t *testing.T) {
	cases := []struct {
		name string
		raw  string
	}{
		{"object not array", `{"overall":"passed"}`},
		{"empty array", `[]`},
		{"nine clauses", `[
			{"path":"p1","op":"exists"},{"path":"p2","op":"exists"},
			{"path":"p3","op":"exists"},{"path":"p4","op":"exists"},
			{"path":"p5","op":"exists"},{"path":"p6","op":"exists"},
			{"path":"p7","op":"exists"},{"path":"p8","op":"exists"},
			{"path":"p9","op":"exists"}
		]`},
		{"unknown clause key", `[{"path":"a","op":"eq","value":1,"junk":true}]`},
		{"missing path", `[{"op":"eq","value":1}]`},
		{"empty path", `[{"path":"","op":"eq","value":1}]`},
		{"missing op", `[{"path":"a","value":1}]`},
		{"unknown op", `[{"path":"a","op":"contains","value":"x"}]`},
		{"eq without value", `[{"path":"a","op":"eq"}]`},
		{"eq null value", `[{"path":"a","op":"eq","value":null}]`},
		{"eq object value", `[{"path":"a","op":"eq","value":{"k":1}}]`},
		{"eq array value", `[{"path":"a","op":"eq","value":[1]}]`},
		{"exists with value", `[{"path":"a","op":"exists","value":1}]`},
		{"ge string value", `[{"path":"a","op":"ge","value":"5"}]`},
		{"le bool value", `[{"path":"a","op":"le","value":true}]`},
		{"ge without value", `[{"path":"a","op":"ge"}]`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := ParseMCPExpect([]byte(tc.raw))
			if code := specCode(err); code != playbook.CodeBadResult {
				t.Fatalf("code = %q, want %q (err=%v)", code, playbook.CodeBadResult, err)
			}
		})
	}
}

func structuredFixture() any {
	return map[string]any{
		"ok":      true,
		"state":   "stopped",
		"overall": "failed",
		"summary": map[string]any{
			"finding_count":  float64(2),
			"all_successful": true,
			"median_wall":    1.5,
		},
		"checks": []any{
			map[string]any{"name": "gdb", "ok": true},
			map[string]any{"name": "root", "ok": false},
		},
		"null_field": nil,
	}
}

func TestCompareMCPClauses(t *testing.T) {
	cases := []struct {
		name string
		raw  string
		ok   bool
	}{
		{"eq string pass", `[{"path":"state","op":"eq","value":"stopped"}]`, true},
		{"eq string fail", `[{"path":"overall","op":"eq","value":"passed"}]`, false},
		{"eq bool pass", `[{"path":"ok","op":"eq","value":true}]`, true},
		{"eq number pass", `[{"path":"summary.finding_count","op":"eq","value":2}]`, true},
		{"eq type mismatch fails", `[{"path":"summary.finding_count","op":"eq","value":"2"}]`, false},
		{"ne same type differing pass", `[{"path":"state","op":"ne","value":"running"}]`, true},
		{"ne equal fails", `[{"path":"state","op":"ne","value":"stopped"}]`, false},
		{"ne type mismatch fails closed", `[{"path":"state","op":"ne","value":1}]`, false},
		{"ge pass", `[{"path":"summary.finding_count","op":"ge","value":2}]`, true},
		{"ge fail", `[{"path":"summary.finding_count","op":"ge","value":3}]`, false},
		{"le pass", `[{"path":"summary.median_wall","op":"le","value":2}]`, true},
		{"le non-number actual fails", `[{"path":"state","op":"le","value":2}]`, false},
		{"exists pass", `[{"path":"summary","op":"exists"}]`, true},
		{"exists null value counts", `[{"path":"null_field","op":"exists"}]`, true},
		{"exists missing fails", `[{"path":"missing","op":"exists"}]`, false},
		{"missing path fails closed", `[{"path":"summary.nope","op":"eq","value":1}]`, false},
		{"array index pass", `[{"path":"checks.1.ok","op":"eq","value":false}]`, true},
		{"array index out of range", `[{"path":"checks.7.ok","op":"exists"}]`, false},
		{"non-integer array segment", `[{"path":"checks.first.ok","op":"exists"}]`, false},
		{"path through scalar fails", `[{"path":"state.deep","op":"exists"}]`, false},
		{"all clauses must hold", `[
			{"path":"ok","op":"eq","value":true},
			{"path":"overall","op":"eq","value":"passed"}
		]`, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			clauses, err := ParseMCPExpect(mustRaw(t, tc.raw))
			if err != nil {
				t.Fatal(err)
			}
			ok, report := CompareMCP(clauses, structuredFixture(), false)
			if ok != tc.ok {
				t.Fatalf("verdict = %v, want %v; report=%+v", ok, tc.ok, report)
			}
			if len(report) != len(clauses) {
				t.Fatalf("report len = %d, want %d", len(report), len(clauses))
			}
			for _, clause := range report {
				if clause.Path == "" || clause.Op == "" {
					t.Fatalf("clause missing path/op: %+v", clause)
				}
			}
		})
	}
}

func TestCompareMCPIsErrorGatesVerdict(t *testing.T) {
	clauses, err := ParseMCPExpect(mustRaw(t, `[{"path":"ok","op":"eq","value":true}]`))
	if err != nil {
		t.Fatal(err)
	}
	ok, report := CompareMCP(clauses, structuredFixture(), true)
	if ok {
		t.Fatal("isError=true must fail the verdict even when every clause holds")
	}
	if len(report) != 1 || !report[0].OK {
		t.Fatalf("clauses still evaluate for review: %+v", report)
	}
}

func TestCompareMCPNilStructuredFailsClosed(t *testing.T) {
	clauses, err := ParseMCPExpect(mustRaw(t, `[{"path":"ok","op":"exists"}]`))
	if err != nil {
		t.Fatal(err)
	}
	if ok, _ := CompareMCP(clauses, nil, false); ok {
		t.Fatal("missing structuredContent must fail closed")
	}
}

func TestValidateMCPExpectClauses(t *testing.T) {
	good := ResultSpec{Kind: "mcp", Server: "perf-mcp", Tool: "perf.measure_command",
		Arguments: map[string]any{"command": []any{"true"}},
		Expect:    mustRaw(t, `[{"path":"summary.all_successful","op":"eq","value":true}]`)}
	if err := Validate(good); err != nil {
		t.Fatalf("well-formed mcp expect rejected: %v", err)
	}
	bad := ResultSpec{Kind: "mcp", Server: "s", Tool: "t",
		Expect: mustRaw(t, `[{"path":"a","op":"contains","value":"x"}]`)}
	if code := specCode(Validate(bad)); code != playbook.CodeBadResult {
		t.Fatalf("bad op: code = %q, want %q", code, playbook.CodeBadResult)
	}
	shell := ResultSpec{Kind: "shell", Command: "true",
		Expect: mustRaw(t, `[{"path":"a","op":"exists"}]`)}
	if code := specCode(Validate(shell)); code != playbook.CodeBadResult {
		t.Fatalf("shell with expect: code = %q, want %q", code, playbook.CodeBadResult)
	}
}
