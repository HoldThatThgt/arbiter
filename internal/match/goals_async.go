package match

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/HoldThatThgt/arbiter/internal/engineclient"
	"github.com/HoldThatThgt/arbiter/internal/playbook"
	"github.com/HoldThatThgt/arbiter/internal/verify"
)

func (s *Store) startAsyncRunGoal(ctx context.Context, spec playbook.ResultSpec, roundSeq int) (CheckStepJobOutput, error) {
	runID, err := s.startRunGoal(ctx, spec)
	if err != nil {
		return CheckStepJobOutput{}, engineUnavailable(err)
	}
	out, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Status != StatusActive || m.Current == nil || m.RoundSeq != roundSeq {
			return nil, CheckStepJobOutput{Complete: false, Reason: "state_changed", RunID: runID}, nil
		}
		v := evaluateRound(m)
		if !v.complete || v.outcome != OutcomeSuccess {
			return nil, CheckStepJobOutput{Complete: false, Reason: "state_changed", RunID: runID}, nil
		}
		m.GoalPending = &GoalPending{
			RoundSeq:  roundSeq,
			RunID:     runID,
			Spec:      spec,
			StartedAt: utcNow(),
		}
		s.append("goal_started", map[string]any{"match_id": m.ID, "round": m.Current.Seq, "run_id": runID, "kind": "run"})
		return m, CheckStepJobOutput{Complete: false, Reason: "goal_running", RunID: runID}, nil
	})
	if err != nil {
		return CheckStepJobOutput{}, err
	}
	return out.(CheckStepJobOutput), nil
}

func (s *Store) pollAsyncRunGoal(ctx context.Context, pending GoalPending) (CheckStepJobOutput, error) {
	report, running, err := s.pollRunGoal(ctx, pending)
	if err != nil {
		return CheckStepJobOutput{}, engineUnavailable(err)
	}
	if running {
		return CheckStepJobOutput{Complete: false, Reason: "goal_running", RunID: pending.RunID}, nil
	}
	out, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Status != StatusActive || m.Current == nil || m.RoundSeq != pending.RoundSeq {
			return nil, CheckStepJobOutput{Complete: false, Reason: "state_changed", RunID: pending.RunID, Goal: report}, nil
		}
		m.GoalPending = nil
		s.append("goal_checked", map[string]any{"match_id": m.ID, "round": m.Current.Seq, "verdict": report.Verdict, "run_id": pending.RunID, "failure": report.Failure})
		v := evaluateRound(m)
		if !v.complete {
			s.append("round_adjudicated", map[string]any{"match_id": m.ID, "round": m.Current.Seq, "step": m.Current.StepID, "complete": false, "reason": v.reason})
			return nil, CheckStepJobOutput{Complete: false, Reason: v.reason, OpenTasks: v.open, Goal: report, RunID: pending.RunID}, nil
		}
		if v.outcome != OutcomeSuccess {
			next, o := s.settle(m, v, false, nil)
			return next, o, nil
		}
		next, o := s.settle(m, v, report.Verdict == TaskPass, report)
		return next, o, nil
	})
	if err != nil {
		return CheckStepJobOutput{}, err
	}
	return out.(CheckStepJobOutput), nil
}

func (s *Store) startRunGoal(ctx context.Context, spec playbook.ResultSpec) (string, error) {
	engine, err := engineclient.Spawn(ctx, engineclient.RoleExec, s.Root)
	if err != nil {
		return "", err
	}
	defer engine.Close()
	started, err := engine.StartRun(ctx, engineRunSpec(spec), nil)
	if err != nil {
		return "", err
	}
	return started.RunID, nil
}

func (s *Store) pollRunGoal(ctx context.Context, pending GoalPending) (*GoalReport, bool, error) {
	engine, err := engineclient.Spawn(ctx, engineclient.RoleExec, s.Root)
	if err != nil {
		return nil, false, err
	}
	defer engine.Close()
	status, err := engine.RunStatus(ctx, pending.RunID)
	if err != nil {
		return nil, false, err
	}
	if status.State == "running" {
		return nil, true, nil
	}
	report, err := runGoalReport(pending, status)
	if err != nil {
		return nil, false, err
	}
	return report, false, nil
}

func engineRunSpec(spec playbook.ResultSpec) map[string]any {
	out := map[string]any{"kind": "run"}
	if spec.Recipe != "" {
		out["recipe"] = spec.Recipe
	}
	if len(spec.Tests) != 0 {
		out["tests"] = append([]string(nil), spec.Tests...)
	}
	if len(spec.Options) != 0 {
		out["options"] = spec.Options
		if result, ok := spec.Options["stub_result"].(map[string]any); ok {
			out["result"] = result
		}
		if sleep, ok := intOption(spec.Options["stub_sleep_ms"]); ok {
			out["sleep_ms"] = sleep
		}
	}
	if len(spec.Expect) != 0 {
		var expect any
		if err := json.Unmarshal(spec.Expect, &expect); err == nil {
			out["expect"] = expect
		}
	}
	if spec.TimeoutS > 0 {
		out["timeout_s"] = spec.TimeoutS
	}
	return out
}

func runGoalReport(pending GoalPending, status engineclient.RunStatus) (*GoalReport, error) {
	payload := struct {
		Overall          string            `json:"overall"`
		Passed           int               `json:"passed"`
		Failed           int               `json:"failed"`
		FirstFailureName string            `json:"first_failure_name"`
		TestResults      map[string]string `json:"test_results"`
		IsError          *bool             `json:"isError"`
		IsErrorSnake     *bool             `json:"is_error"`
		Failure          string            `json:"failure"`
	}{}
	if len(status.Result) != 0 {
		if err := json.Unmarshal(status.Result, &payload); err != nil {
			return nil, err
		}
	}
	if payload.Overall == "" {
		if status.State == "completed" {
			payload.Overall = "passed"
		} else {
			payload.Overall = "failed"
		}
	}
	isError := payload.IsError
	if isError == nil {
		isError = payload.IsErrorSnake
	}
	expect, err := verify.ParseRunExpect(pending.Spec.Expect)
	if err != nil {
		return nil, err
	}
	ok, _ := verify.CompareRun(expect, verify.RunEvidence{
		RunID:            pending.RunID,
		Overall:          payload.Overall,
		Passed:           payload.Passed,
		Failed:           payload.Failed,
		FirstFailureName: payload.FirstFailureName,
		TestResults:      payload.TestResults,
	})
	verdict := TaskFail
	if ok {
		verdict = TaskPass
	}
	return &GoalReport{
		Verdict: verdict,
		RunID:   pending.RunID,
		IsError: isError,
		Output:  string(status.Result),
		Failure: payload.Failure,
	}, nil
}

func intOption(value any) (int, bool) {
	switch n := value.(type) {
	case int:
		return n, true
	case float64:
		return int(n), n == float64(int(n))
	default:
		return 0, false
	}
}

func engineUnavailable(err error) error {
	return &ToolError{Code: playbook.CodeEngineUnavailable, Message: fmt.Sprintf("engine unavailable: %v", err)}
}
