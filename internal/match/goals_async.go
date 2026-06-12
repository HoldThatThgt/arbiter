package match

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/HoldThatThgt/arbiter/internal/engineclient"
	"github.com/HoldThatThgt/arbiter/internal/playbook"
	"github.com/HoldThatThgt/arbiter/internal/verify"
)

func (s *Store) startAsyncRunGoal(ctx context.Context, spec playbook.ResultSpec, roundSeq int, memoDigest string) (CheckStepJobOutput, error) {
	// 起跑前的完整性闸:引擎 worker 在仓内"当前字节"上编译并运行冻结测试。若此刻
	// 测试已被旁路 guard 的 Bash 改写,直接判负、绝不开跑——否则 worker 跑的是被弱化
	// 的套件。这与 poll/settle 时的复核一道,把"起跑前篡改"挡在引擎之外。
	// (开跑后趁 worker 异步编译窗口篡改、于下次 poll 前复原的竞态,由 worker 上报
	//  "编译前一刻"实测摘要根除:这里把待核验的冻结测试路径快照交给引擎,worker
	//  在真正编译前对其逐一取摘要回报,落子前于 pollAsyncRunGoal 与冻结登记表比对。)
	var frozen []string
	gate, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Status != StatusActive || m.Current == nil || m.RoundSeq != roundSeq {
			return nil, CheckStepJobOutput{Complete: false, Reason: "state_changed"}, nil
		}
		v := evaluateRound(m)
		if !v.complete || v.outcome != OutcomeSuccess {
			return nil, CheckStepJobOutput{Complete: false, Reason: "state_changed"}, nil
		}
		if violated := frozenViolation(s.Root, m.FrozenTests); violated != "" {
			next, o := s.settle(m, v, false, frozenGoalReport(violated))
			return next, o, nil
		}
		frozen = frozenPaths(m.FrozenTests) // worker 在编译前一刻据此实测、回报摘要
		return nil, nil, nil                // 闸通过,照常开跑
	})
	if err != nil {
		return CheckStepJobOutput{}, err
	}
	if gate != nil {
		return gate.(CheckStepJobOutput), nil
	}

	runID, err := s.startRunGoal(ctx, spec, frozen)
	if err != nil {
		return CheckStepJobOutput{}, engineUnavailable(err)
	}
	discarded := false
	out, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Status != StatusActive || m.Current == nil || m.RoundSeq != roundSeq {
			discarded = true
			return nil, CheckStepJobOutput{Complete: false, Reason: "state_changed", RunID: runID}, nil
		}
		v := evaluateRound(m)
		if !v.complete || v.outcome != OutcomeSuccess {
			discarded = true
			return nil, CheckStepJobOutput{Complete: false, Reason: "state_changed", RunID: runID}, nil
		}
		m.GoalPending = &GoalPending{
			RoundSeq:   roundSeq,
			RunID:      runID,
			Spec:       spec,
			MemoDigest: memoDigest,
			StartedAt:  utcNow(),
		}
		s.append("goal_started", map[string]any{"match_id": m.ID, "round": m.Current.Seq, "run_id": runID, "kind": "run"})
		return m, CheckStepJobOutput{Complete: false, Reason: "goal_running", RunID: runID}, nil
	})
	if err != nil {
		// 状态层面的终局错误:这次 goal 生命周期已经结束,关掉缓存引擎。
		s.closeGoalEngine()
		return CheckStepJobOutput{}, err
	}
	if discarded {
		// run 已启动但 pending 被弃置(state_changed):没有任何后续 poll 会
		// 复用这台引擎,立即关闭。
		s.closeGoalEngine()
	}
	return out.(CheckStepJobOutput), nil
}

func (s *Store) pollAsyncRunGoal(ctx context.Context, pending GoalPending) (CheckStepJobOutput, error) {
	report, running, err := s.pollRunGoal(ctx, pending)
	if err != nil {
		// 可重试(engine_unavailable):GoalPending 原样保留,缓存引擎也保留——
		// 协议层失败已使其中毒,下次 poll 由 goalExecEngine 原地重生。
		return CheckStepJobOutput{}, engineUnavailable(err)
	}
	if running {
		return CheckStepJobOutput{Complete: false, Reason: "goal_running", RunID: pending.RunID}, nil
	}
	// run 已达终态:下面每个分支要么 settle 要么弃置这个 pending,
	// goal 生命周期就此结束,缓存引擎随之关闭。
	defer s.closeGoalEngine()
	out, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Status != StatusActive || m.Current == nil || m.RoundSeq != pending.RoundSeq {
			return nil, CheckStepJobOutput{Complete: false, Reason: "state_changed", RunID: pending.RunID, Goal: report}, nil
		}
		m.GoalPending = nil
		// 冻结测试完整性闸(异步将死路径),两道复核叠加:
		// (1) 落子前重算磁盘哈希 —— 抓住 run 结束后的篡改,以及 run 期间新冻结的测试
		//     (与同步路径、SubmitTask 同源)。
		// (2) 比对 worker 在"编译前一刻"实测上报的摘要 —— 抓住磁盘此刻已复原、但
		//     worker 实际编译的正是被弱化字节的竞态(通关→弱化→编译→复原→poll)。
		//     这是 (1) 的磁盘复算结构上看不到的:它只能看见复原后的盘面。
		if violated := frozenViolation(s.Root, m.FrozenTests); violated != "" {
			report = frozenGoalReport(violated)
		} else if violated := frozenDigestViolation(m.FrozenTests, report.frozenDigests); violated != "" {
			report = frozenGoalReport(violated)
		}
		if pending.MemoDigest != "" {
			// TOCTOU 防线:run 执行期间工作区可能已被改写。重算摘要,
			// 只有与执行前一致(工作区未变)才记入 memo;否则静默跳过。
			if digest, digestErr := s.goalMemoDigest(m, pending.Spec); digestErr == nil && digest == pending.MemoDigest {
				rememberGoalMemo(m, pending.MemoDigest, report)
			}
		}
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

// startRunGoal / pollRunGoal 共用 Store 缓存的 exec 引擎(goals_engine.go):
// GoalPending 存续期间每次 CheckStepJob poll 不再各付一次解释器启动开销。
func (s *Store) startRunGoal(ctx context.Context, spec playbook.ResultSpec, frozen []string) (string, error) {
	engine, err := s.goalExecEngine(ctx)
	if err != nil {
		return "", err
	}
	started, err := engine.StartRun(ctx, engineRunSpec(spec, frozen), nil)
	if err != nil {
		return "", err
	}
	return started.RunID, nil
}

func (s *Store) pollRunGoal(ctx context.Context, pending GoalPending) (*GoalReport, bool, error) {
	engine, err := s.goalExecEngine(ctx)
	if err != nil {
		return nil, false, err
	}
	status, err := engine.RunStatus(ctx, pending.RunID)
	if err != nil {
		return nil, false, err
	}
	return resolveRunGoalStatus(pending, status)
}

// resolveRunGoalStatus 把引擎的 runStatus 翻译成裁决结论。
// 只有终态(completed/failed)产出 GoalReport;"unknown"(引擎查无此 run 行)
// 或任何未识别状态都按引擎不可用返回错误 —— 由调用方包装成可重试的
// engine_unavailable ToolError,GoalPending 原样保留,绝不伪造 Overall="failed" 定局。
func resolveRunGoalStatus(pending GoalPending, status engineclient.RunStatus) (*GoalReport, bool, error) {
	switch status.State {
	case "running":
		return nil, true, nil
	case "completed", "failed":
		report, err := runGoalReport(pending, status)
		if err != nil {
			return nil, false, err
		}
		return report, false, nil
	default:
		return nil, false, fmt.Errorf("engine run %s reported unrecognized state %q", pending.RunID, status.State)
	}
}

func engineRunSpec(spec playbook.ResultSpec, frozen []string) map[string]any {
	out := map[string]any{"kind": "run"}
	if spec.Recipe != "" {
		out["recipe"] = spec.Recipe
	}
	if len(spec.Tests) != 0 {
		out["tests"] = append([]string(nil), spec.Tests...)
	}
	// 把待核验的冻结测试路径交给 worker:它在编译前一刻据此实测内容摘要回报,
	// 让裁决能反映"实际编译的字节"(见 _hash_frozen / frozenDigestViolation)。
	if len(frozen) != 0 {
		out["frozen"] = append([]string(nil), frozen...)
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
		Overall          string                   `json:"overall"`
		Passed           int                      `json:"passed"`
		Failed           int                      `json:"failed"`
		FirstFailureName string                   `json:"first_failure_name"`
		TestResults      map[string]string        `json:"test_results"`
		PerTest          []verify.RunPerTest      `json:"per_test"`
		Facts            *verify.RunFactsEvidence `json:"facts"`
		IsError          *bool                    `json:"isError"`
		IsErrorSnake     *bool                    `json:"is_error"`
		Failure          string                   `json:"failure"`
		FrozenDigests    map[string]string        `json:"frozen_digests"`
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
	// 规范形态是引擎的 per_test 数组(gtest.py RunResult.to_json):
	// 摊平成 expect.test.name 使用的 "Suite.Name" → status;legacy 的
	// test_results 键仍接受,且按原行为覆盖同名条目。
	testResults := verify.RunTestResults(payload.PerTest)
	if testResults == nil {
		testResults = payload.TestResults
	} else {
		for name, result := range payload.TestResults {
			testResults[name] = result
		}
	}
	if payload.FirstFailureName == "" {
		payload.FirstFailureName = verify.FirstRunFailure(payload.PerTest)
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
		TestResults:      testResults,
		Facts:            payload.Facts,
	})
	verdict := TaskFail
	if ok {
		verdict = TaskPass
	}
	return &GoalReport{
		Verdict:       verdict,
		RunID:         pending.RunID,
		IsError:       isError,
		Output:        string(status.Result),
		Failure:       payload.Failure,
		frozenDigests: payload.FrozenDigests,
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
