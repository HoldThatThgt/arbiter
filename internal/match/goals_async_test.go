package match

import (
	"encoding/json"
	"testing"

	"github.com/HoldThatThgt/arbiter/internal/engineclient"
	"github.com/HoldThatThgt/arbiter/internal/playbook"
)

func runGoalPending(t *testing.T, expect string) GoalPending {
	t.Helper()
	if !json.Valid([]byte(expect)) {
		t.Fatalf("invalid expect fixture: %s", expect)
	}
	return GoalPending{
		RunID: "r1",
		Spec: playbook.ResultSpec{
			Kind:   "run",
			Recipe: "unit",
			Tests:  []string{"Suite.Case"},
			Expect: json.RawMessage(expect),
		},
	}
}

// 引擎 run 结果的规范形态:per_test 数组(gtest.py RunResult.to_json)。
// test 子句按 "Suite.Case" 命名必须命中 per_test 摊平后的映射。
func TestRunGoalReportDecodesEnginePerTest(t *testing.T) {
	pending := runGoalPending(t, `{"test":{"name":"Suite.Case","result":"passed"}}`)
	result := `{
		"run_id": "r1",
		"overall": "passed",
		"passed": 1,
		"failed": 0,
		"skipped": 0,
		"per_test": [
			{"suite": "Suite", "name": "Case", "occurrence": 1, "status": "passed", "elapsed_ms": 3}
		],
		"isError": false,
		"content": [{"type": "text", "text": "unit: passed"}]
	}`
	report, err := runGoalReport(pending, engineclient.RunStatus{RunID: "r1", State: "completed", Result: json.RawMessage(result)})
	if err != nil {
		t.Fatal(err)
	}
	if report.Verdict != TaskPass {
		t.Fatalf("verdict = %q, want %q (report=%#v)", report.Verdict, TaskPass, report)
	}
	if report.IsError == nil || *report.IsError {
		t.Fatalf("isError = %#v", report.IsError)
	}
}

func TestRunGoalReportPerTestFailureFailsTestClause(t *testing.T) {
	pending := runGoalPending(t, `{"test":{"name":"Suite.Case","result":"passed"}}`)
	result := `{
		"run_id": "r1",
		"overall": "failed",
		"passed": 0,
		"failed": 1,
		"skipped": 0,
		"per_test": [
			{"suite": "Suite", "name": "Case", "occurrence": 1, "status": "failed", "elapsed_ms": 3, "message": "boom"}
		],
		"isError": false
	}`
	report, err := runGoalReport(pending, engineclient.RunStatus{RunID: "r1", State: "completed", Result: json.RawMessage(result)})
	if err != nil {
		t.Fatal(err)
	}
	if report.Verdict != TaskFail {
		t.Fatalf("verdict = %q, want %q", report.Verdict, TaskFail)
	}
}

// legacy 的 test_results 键仍被接受。
func TestRunGoalReportAcceptsLegacyTestResults(t *testing.T) {
	pending := runGoalPending(t, `{"test":{"name":"Suite.Case","result":"passed"}}`)
	result := `{"overall": "passed", "passed": 1, "failed": 0, "test_results": {"Suite.Case": "passed"}}`
	report, err := runGoalReport(pending, engineclient.RunStatus{RunID: "r1", State: "completed", Result: json.RawMessage(result)})
	if err != nil {
		t.Fatal(err)
	}
	if report.Verdict != TaskPass {
		t.Fatalf("verdict = %q, want %q", report.Verdict, TaskPass)
	}
}

// state "unknown"(引擎查无此 run 行)或未识别状态绝不伪造 Overall="failed" 定局:
// 返回错误,由 pollAsyncRunGoal 包装成可重试的 engine_unavailable,GoalPending 保留。
func TestResolveRunGoalStatusUnknownStateIsRetryable(t *testing.T) {
	for _, state := range []string{"unknown", "surprise"} {
		report, running, err := resolveRunGoalStatus(runGoalPending(t, `{"overall":"passed"}`), engineclient.RunStatus{RunID: "r1", State: state})
		if err == nil || running || report != nil {
			t.Fatalf("state %q: report=%#v running=%v err=%v", state, report, running, err)
		}
	}
}

func TestResolveRunGoalStatusRunningKeepsPolling(t *testing.T) {
	report, running, err := resolveRunGoalStatus(runGoalPending(t, `{"overall":"passed"}`), engineclient.RunStatus{RunID: "r1", State: "running"})
	if err != nil || !running || report != nil {
		t.Fatalf("report=%#v running=%v err=%v", report, running, err)
	}
}
