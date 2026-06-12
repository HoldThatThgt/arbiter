package match

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/HoldThatThgt/arbiter/internal/engineclient"
	"github.com/HoldThatThgt/arbiter/internal/playbook"
	"github.com/HoldThatThgt/arbiter/internal/verify"
)

const (
	maxFactRefs      = 8
	maxBriefingBytes = 8 * 1024
)

func (s *Store) ReadPlayBook() (map[string]any, error) {
	cat := playbook.ScanDir(s.playbookDir())
	books := []map[string]any{}
	for _, entry := range cat.Entries {
		if len(entry.Problems) > 0 {
			continue
		}
		var steps []playbook.StepView
		for _, step := range entry.Book.OrderedSteps() {
			steps = append(steps, playbook.StepView{
				ID:        step.ID,
				Job:       step.Job,
				Checklist: step.Checklist,
				Gotchas:   step.Gotchas,
				Branch:    step.Branch,
			})
		}
		books = append(books, map[string]any{
			"name":        entry.Book.Name,
			"description": entry.Book.Description,
			"entry":       entry.Book.Entry,
			"steps":       steps,
		})
	}
	invalid := cat.Invalid
	if invalid == nil {
		invalid = []playbook.Issue{}
	}
	return map[string]any{"playbooks": books, "invalid": invalid}, nil
}

func (s *Store) ActiveCapabilities() ([]string, error) {
	out, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Status != StatusActive {
			return nil, []string{}, nil
		}
		return nil, append([]string(nil), m.Playbook.Capabilities...), nil
	})
	if err != nil {
		return nil, err
	}
	return out.([]string), nil
}

func (s *Store) RequireActiveCapability(capability string) error {
	_, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Status != StatusActive || !hasCapability(m.Playbook.Capabilities, capability) {
			return nil, nil, &ToolError{Code: playbook.CodeCapabilityRevoked, Message: "capability revoked: the match that granted this tool changed or ended — stop this task and report back to the player"}
		}
		return nil, nil, nil
	})
	return err
}

func (s *Store) CurrentMeta() map[string]any {
	out, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Status != StatusActive || m.Current == nil {
			return nil, map[string]any{}, nil
		}
		return nil, map[string]any{"match_id": m.ID, "round": m.Current.Seq}, nil
	})
	if err != nil {
		return map[string]any{}
	}
	return out.(map[string]any)
}

func hasCapability(values []string, target string) bool {
	for _, value := range values {
		if value == target {
			return true
		}
	}
	return false
}

func (s *Store) LoadPlayBook(name string) (LoadPlayBookOutput, error) {
	cat := playbook.ScanDir(s.playbookDir())
	entry, code := cat.Find(name)
	if code == playbook.CodePlaybookNotFound {
		return LoadPlayBookOutput{}, &ToolError{Code: code, Message: "playbook not found — choose one of data.available, or freeplay as the generic fallback", Data: map[string]any{"available": cat.LoadableNames()}}
	}
	if code == playbook.CodeNameConflict {
		return LoadPlayBookOutput{}, &ToolError{Code: code, Message: "playbook name conflict — that intent already has a book; extend it or register under a genuinely different intent name", Data: map[string]any{"name": name}}
	}
	if code == playbook.CodePlaybookInvalid {
		return LoadPlayBookOutput{}, &ToolError{Code: code, Message: "playbook invalid — fix exactly what data.issues lists, then retry", Data: map[string]any{"issues": entry.Problems}}
	}
	if len(entry.Problems) > 0 {
		return LoadPlayBookOutput{}, &ToolError{Code: playbook.CodePlaybookInvalid, Message: "playbook invalid — fix exactly what data.issues lists, then retry", Data: map[string]any{"issues": entry.Problems}}
	}
	recipesPin, err := s.currentRecipesPin()
	if err != nil {
		return LoadPlayBookOutput{}, &ToolError{Code: playbook.CodeRecipePinMismatch, Message: "recipe pin mismatch — recipes.yaml changed after LoadPlayBook pinned it; finish or reload the match before editing recipes", Data: map[string]any{"error": err.Error()}}
	}

	out, err := s.withLock(func(current *Match) (*Match, any, error) {
		var replaced *string
		if current != nil && current.Status == StatusActive {
			old := current.ID
			replaced = &old
			s.append("match_replaced", map[string]any{"match_id": old, "old_match_id": old})
		}
		now := time.Now().UTC()
		m := &Match{
			ID:         newMatchID(now),
			Playbook:   entry.Book,
			RecipesPin: recipesPin,
			// 具名谓词随策略一起封盘进对局快照(深拷贝),镜像 RecipePin 信任模型。
			VerifyPolicy: entry.Book.VerifyPolicy,
			VerifySpecs:  cloneVerifySpecs(entry.Book.Verify),
			Status:       StatusActive,
			Current:      &Round{Seq: 1, StepID: entry.Book.Entry, EnteredAt: now.Format(time.RFC3339)},
			History:      []Round{},
			RoundSeq:     1,
			StartedAt:    now.Format(time.RFC3339),
		}
		s.append("match_started", map[string]any{"match_id": m.ID, "playbook": m.Playbook.Name, "entry": m.Playbook.Entry})
		s.append("round_entered", map[string]any{"match_id": m.ID, "round": 1, "step": m.Playbook.Entry})
		return m, LoadPlayBookOutput{
			MatchID:       m.ID,
			Playbook:      m.Playbook.Name,
			FirstStep:     m.Playbook.Entry,
			StepsTotal:    len(m.Playbook.Steps),
			ReplacedMatch: replaced,
		}, nil
	})
	if err != nil {
		return LoadPlayBookOutput{}, err
	}
	return out.(LoadPlayBookOutput), nil
}

func (s *Store) ShowStepJob() (ShowStepJobOutput, error) {
	out, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil {
			return nil, ShowStepJobOutput{Status: "idle", Hint: "无活动对局,请先通过 arbiter-curator 装载棋谱"}, nil
		}
		if m.Status != StatusActive {
			return nil, ShowStepJobOutput{Status: m.Status, Playbook: m.Playbook.Name, Rounds: len(m.History), Abort: m.Abort}, nil
		}
		step := m.Playbook.Steps[m.Current.StepID]
		tasks := make([]TaskSummary, 0, len(m.Current.Tasks))
		for _, task := range m.Current.Tasks {
			tasks = append(tasks, TaskSummary{ID: task.ID, Status: task.Status, Request: task.Request, Summary: task.Summary})
		}
		return nil, ShowStepJobOutput{
			Status:   StatusActive,
			Playbook: m.Playbook.Name,
			Round:    m.Current.Seq,
			Step:     &StepOutput{ID: step.ID, Job: step.Job, Checklist: step.Checklist, Gotchas: step.Gotchas, Submit: step.Submit},
			Tasks:    tasks,
			Verify:   verifyDecls(m.VerifySpecs),
		}, nil
	})
	if err != nil {
		return ShowStepJobOutput{}, err
	}
	return out.(ShowStepJobOutput), nil
}

func (s *Store) CreateTask(request string) (CreateTaskOutput, error) {
	return s.CreateTaskWithFacts(request, nil)
}

func (s *Store) CreateTaskWithFacts(request string, factRefs []string) (CreateTaskOutput, error) {
	request = strings.TrimSpace(request)
	if request == "" {
		return CreateTaskOutput{}, &ToolError{Code: playbook.CodeEmptyRequest, Message: "request is empty — write a self-contained instruction: goal, scope, and the exact result predicate the executor must submit"}
	}
	briefing, err := s.resolveBriefing(factRefs)
	if err != nil {
		return CreateTaskOutput{}, err
	}
	out, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Status != StatusActive || m.Current == nil {
			return nil, nil, &ToolError{Code: playbook.CodeNoActiveMatch, Message: "no active match — the curator must LoadPlayBook first; match tools only work inside a loaded match"}
		}
		m.TaskSeq++
		task := Task{ID: fmt.Sprintf("T%d", m.TaskSeq), Request: request, Status: TaskOpen, Briefing: briefing}
		m.Current.Tasks = append(m.Current.Tasks, task)
		fields := map[string]any{"match_id": m.ID, "task": task.ID, "request": task.Request}
		if len(briefing) > 0 {
			fields["briefing"] = briefing
		}
		s.append("task_created", fields)
		return m, CreateTaskOutput{TaskID: task.ID, StepID: m.Current.StepID}, nil
	})
	if err != nil {
		return CreateTaskOutput{}, err
	}
	return out.(CreateTaskOutput), nil
}

func (s *Store) resolveBriefing(factRefs []string) ([]BriefingCard, error) {
	if len(factRefs) == 0 {
		return nil, nil
	}
	if len(factRefs) > maxFactRefs {
		return nil, &ToolError{Code: playbook.CodeBriefingUnresolved, Message: "briefing unresolved — the listed fact_refs did not resolve; use object ids returned by search, fix or drop data.bad_refs", Data: map[string]any{"bad_refs": factRefs}}
	}
	for _, ref := range factRefs {
		if strings.TrimSpace(ref) == "" {
			return nil, &ToolError{Code: playbook.CodeBriefingUnresolved, Message: "briefing unresolved — the listed fact_refs did not resolve; use object ids returned by search, fix or drop data.bad_refs", Data: map[string]any{"bad_refs": []string{ref}}}
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(playbook.DefaultTimeoutS)*time.Second)
	defer cancel()
	engine, err := engineclient.Spawn(ctx, engineclient.RoleQuery, s.Root)
	if err != nil {
		return nil, &ToolError{Code: playbook.CodeEngineUnavailable, Message: "engine unavailable: " + err.Error()}
	}
	defer engine.Close()
	resolved, err := engine.ResolveBriefing(ctx, factRefs, map[string]any{"purpose": "briefing"})
	if err != nil {
		var engineErr *engineclient.EngineError
		if errors.As(err, &engineErr) && engineErr.Kind == playbook.CodeBriefingUnresolved {
			return nil, &ToolError{Code: playbook.CodeBriefingUnresolved, Message: "briefing unresolved — the listed fact_refs did not resolve; use object ids returned by search, fix or drop data.bad_refs", Data: jsonRawObject(engineErr.Data)}
		}
		return nil, &ToolError{Code: playbook.CodeEngineUnavailable, Message: "engine unavailable: " + err.Error()}
	}
	briefing := make([]BriefingCard, 0, len(resolved.Briefing))
	for _, card := range resolved.Briefing {
		briefing = append(briefing, BriefingCard{Ref: card.Ref, Content: card.Content})
	}
	data, err := json.Marshal(briefing)
	if err != nil {
		return nil, err
	}
	if len(data) > maxBriefingBytes {
		return nil, &ToolError{Code: playbook.CodeBriefingUnresolved, Message: "briefing unresolved — the listed fact_refs did not resolve; use object ids returned by search, fix or drop data.bad_refs", Data: map[string]any{"reason": "briefing_too_large"}}
	}
	return briefing, nil
}

func jsonRawObject(raw json.RawMessage) map[string]any {
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		return map[string]any{}
	}
	return out
}

func (s *Store) SubmitTask(ctx context.Context, taskID, summary, report string, spec verify.ResultSpec) (SubmitTaskOutput, error) {
	summary = strings.TrimSpace(summary)
	if summary == "" {
		return SubmitTaskOutput{}, &ToolError{Code: playbook.CodeBadSummary, Message: "summary is empty — one line for the global task ledger (what the outcome was, not what you did)"}
	}
	if len(summary) > playbook.MaxSummaryBytes {
		return SubmitTaskOutput{}, &ToolError{Code: playbook.CodeBadSummary, Message: fmt.Sprintf("summary exceeds %d bytes", playbook.MaxSummaryBytes)}
	}
	var roundSeq int
	var matchID string
	var verifyName string
	var frozen map[string]string
	out, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Status != StatusActive || m.Current == nil {
			return nil, nil, &ToolError{Code: playbook.CodeNoActiveMatch, Message: "no active match — the curator must LoadPlayBook first; match tools only work inside a loaded match"}
		}
		// 步骤强绑定([Submit]):本步骤若钉死了谓词,提交必须正是它 —— 在解析
		// 之前比对原始提交,连"选哪条 curated 谓词/塞内联 spec"的自由都不给,
		// 模型挑一条更弱谓词蒙混的路被堵死。allow_overrides 范围内的覆盖随谓词
		// 一起带,由后续 resolveVerifySpec 裁定。
		if required := m.Playbook.Steps[m.Current.StepID].Submit; required != "" && spec.Verify != required {
			return nil, nil, &ToolError{
				Code:    playbook.CodeStepSubmitMismatch,
				Message: fmt.Sprintf("this step is bound to result {\"verify\": %q} — submit exactly that (only its allowed overrides may ride along); the predicate is the step's to dictate, not yours to choose", required),
				Data:    map[string]any{"required": required, "submitted_verify": spec.Verify},
			}
		}
		// verify 引用在锁内对照对局快照解析成 curated spec(绝不读棋谱文件),
		// 解析结果照常流经 Validate → recipe pin → ExecuteWithMeta。
		resolved, name, err := resolveVerifySpec(m, spec)
		if err != nil {
			return nil, nil, err
		}
		spec = resolved
		verifyName = name
		if err := verify.Validate(spec); err != nil {
			return nil, nil, specError(err)
		}
		if spec.Kind == "run" {
			if err := s.checkRecipePin(m, spec); err != nil {
				return nil, nil, err
			}
		}
		if _, ok := findCurrentTask(m, taskID); ok {
			roundSeq = m.RoundSeq
			matchID = m.ID
			frozen = copyStringMap(m.FrozenTests)
			return nil, nil, nil
		}
		if findHistoryTask(m, taskID) != nil {
			return nil, nil, &ToolError{Code: playbook.CodeTaskStale, Message: "task belongs to an archived round — the match moved on; call ListTask for the live ledger before acting"}
		}
		return nil, nil, &ToolError{Code: playbook.CodeTaskNotFound, Message: "task not found — verify the id against ListTask; ids are match-scoped and case-sensitive"}
	})
	if err != nil {
		return SubmitTaskOutput{}, err
	}
	_ = out

	// 冻结测试完整性闸:任何谓词执行前先核对已注册测试未被改动。一旦改动,
	// 不论经由何种途径(guard 旁路、git、sed、脚本……),本次提交直接判负,
	// 谓词都不必跑 —— tampered 测试不可能换来 pass。
	var result verify.Result
	if violated := frozenViolation(s.Root, frozen); violated != "" {
		result = frozenViolationResult(spec, violated)
	} else {
		var execErr error
		result, execErr = verify.ExecuteWithMeta(ctx, s.Root, spec, map[string]any{"match_id": matchID, "round_seq": roundSeq})
		if execErr != nil {
			return SubmitTaskOutput{}, specError(execErr)
		}
		// 执行后再核对一次:谓词自身的副作用(如 shell 谓词里 sed/cp 改写)可能在
		// 前置检查通过之后篡改冻结测试,再跑一个被弱化的套件蒙混。执行后内容一变,
		// 本次裁决直接判负 —— 谓词的 pass 不作数。
		if violated := frozenViolation(s.Root, frozen); violated != "" {
			result = frozenViolationResult(spec, violated)
		}
	}
	verdict := TaskFail
	if verify.Pass(result) {
		verdict = TaskPass
	}

	out, err = s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Status != StatusActive || m.Current == nil {
			return nil, nil, &ToolError{Code: playbook.CodeTaskStale, Message: "task belongs to an archived round — the match moved on; call ListTask for the live ledger before acting"}
		}
		if m.RoundSeq != roundSeq {
			s.append("task_submitted", map[string]any{"match_id": m.ID, "task": taskID, "verdict": "stale"})
			return nil, nil, &ToolError{Code: playbook.CodeTaskStale, Message: "task belongs to an archived round — the match moved on; call ListTask for the live ledger before acting"}
		}
		idx, ok := findCurrentTask(m, taskID)
		if !ok {
			return nil, nil, &ToolError{Code: playbook.CodeTaskStale, Message: "task belongs to an archived round — the match moved on; call ListTask for the live ledger before acting"}
		}
		m.Current.Tasks[idx].Summary = summary
		m.Current.Tasks[idx].Report = report
		m.Current.Tasks[idx].Result = &result
		m.Current.Tasks[idx].Status = verdict
		fields := map[string]any{
			"match_id":    m.ID,
			"task":        taskID,
			"verdict":     verdict,
			"summary":     summary,
			"spec":        result.Spec,
			"duration_ms": result.DurationMS,
			"output":      result.Output,
		}
		if verifyName != "" {
			fields["verify"] = verifyName // 台账记下这次裁决出自哪个 curated 谓词
		}
		if result.ExitCode != nil {
			fields["exit_code"] = *result.ExitCode
		}
		if result.IsError != nil {
			fields["is_error"] = *result.IsError
		}
		if result.Failure != "" {
			fields["failure"] = result.Failure // frozen_test_modified 等失败码入账,台账可审计
		}
		s.append("task_submitted", fields)
		return m, SubmitTaskOutput{
			TaskID:     taskID,
			Verdict:    verdict,
			ExitCode:   result.ExitCode,
			IsError:    result.IsError,
			Output:     result.Output,
			DurationMS: result.DurationMS,
			Failure:    result.Failure,
		}, nil
	})
	if err != nil {
		return SubmitTaskOutput{}, err
	}
	return out.(SubmitTaskOutput), nil
}

// frozenModifiedMessage 是冻结测试被改动时回报给执行者的统一说明。
func frozenModifiedMessage(violated string) string {
	return "registered test was modified and is immutable: " + violated + " — restore it byte-for-byte; fixes go in product code, never the test"
}

// frozenViolationResult 合成一个谓词未跑、直接判负的裁决结果(冻结测试被改)。
func frozenViolationResult(spec verify.ResultSpec, violated string) verify.Result {
	return verify.Result{Spec: spec, Failure: playbook.CodeFrozenTestModified, Output: frozenModifiedMessage(violated)}
}

// frozenGoalReport 合成一个 goal/将死谓词判负的报告(冻结测试被改)。
func frozenGoalReport(violated string) *GoalReport {
	return &GoalReport{Verdict: TaskFail, Failure: playbook.CodeFrozenTestModified, Output: frozenModifiedMessage(violated)}
}

// roundVerdict 是裁决的纯计算结果(不含状态变更)。
type roundVerdict struct {
	complete bool
	reason   string
	open     []string
	outcome  string
	target   string
}

func evaluateRound(m *Match) roundVerdict {
	if len(m.Current.Tasks) == 0 {
		return roundVerdict{reason: "no_tasks"}
	}
	var open []string
	hasFail := false
	for _, task := range m.Current.Tasks {
		switch task.Status {
		case TaskOpen:
			open = append(open, task.ID)
		case TaskFail:
			hasFail = true
		}
	}
	if len(open) > 0 {
		return roundVerdict{reason: "open_tasks", open: open}
	}
	step := m.Playbook.Steps[m.Current.StepID]
	if hasFail {
		return roundVerdict{complete: true, outcome: OutcomeFailure, target: step.Branch.Failure}
	}
	return roundVerdict{complete: true, outcome: OutcomeSuccess, target: step.Branch.Success}
}

// settle 在锁内归档当前回合并推进/终局。checkmate 表示 goal 谓词已通过。
func (s *Store) settle(m *Match, v roundVerdict, checkmate bool, goal *GoalReport) (*Match, CheckStepJobOutput) {
	archived := *m.Current
	for index := range archived.Tasks {
		archived.Tasks[index].Briefing = nil
	}
	archived.Outcome = v.outcome
	m.History = append(m.History, archived)
	m.Current = nil
	m.GoalPending = nil
	s.append("round_adjudicated", map[string]any{"match_id": m.ID, "round": archived.Seq, "step": archived.StepID, "complete": true, "outcome": v.outcome, "target": v.target})

	out := CheckStepJobOutput{Complete: true, Outcome: v.outcome, Goal: goal}
	if checkmate {
		m.Status = StatusFinishedSuccess
		out.Checkmate = true
		out.Match = m.Status
		s.append("match_finished", map[string]any{"match_id": m.ID, "outcome": OutcomeSuccess, "checkmate": true})
		return m, out
	}
	if v.target == playbook.EndTarget {
		switch {
		case v.outcome == OutcomeSuccess && m.Playbook.Goal != nil:
			// 走到 END 但未将死:goal 是唯一的胜利判据
			m.Status = StatusFinishedFailure
			s.append("match_finished", map[string]any{"match_id": m.ID, "outcome": OutcomeFailure, "reason": "goal_unmet"})
		case v.outcome == OutcomeSuccess:
			m.Status = StatusFinishedSuccess
			s.append("match_finished", map[string]any{"match_id": m.ID, "outcome": OutcomeSuccess})
		default:
			m.Status = StatusFinishedFailure
			s.append("match_finished", map[string]any{"match_id": m.ID, "outcome": OutcomeFailure})
		}
		out.Match = m.Status
		return m, out
	}
	nextSeq := m.RoundSeq + 1
	if nextSeq > m.Playbook.StepBudget() {
		m.Status = StatusAborted
		m.Abort = AbortStepsExhausted
		s.append("match_aborted", map[string]any{"match_id": m.ID, "abort": AbortStepsExhausted, "rounds": m.RoundSeq})
		out.Match = StatusAborted
		out.Abort = AbortStepsExhausted
		return m, out
	}
	m.RoundSeq = nextSeq
	m.StopBlocks = 0
	m.SubagentBlocks = 0
	m.Current = &Round{Seq: nextSeq, StepID: v.target, EnteredAt: utcNow()}
	s.append("round_entered", map[string]any{"match_id": m.ID, "round": nextSeq, "step": v.target})
	out.NextStep = v.target
	out.Round = nextSeq
	return m, out
}

func (s *Store) CheckStepJob(ctx context.Context) (CheckStepJobOutput, error) {
	var pendingGoal *playbook.ResultSpec
	var pendingSeq int
	var pendingDigest string
	var startRunGoal *playbook.ResultSpec
	var startRunSeq int
	var startRunDigest string
	var pollRunGoal *GoalPending
	var discardedPending bool
	out, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Status != StatusActive || m.Current == nil {
			return nil, nil, &ToolError{Code: playbook.CodeNoActiveMatch, Message: "no active match — the curator must LoadPlayBook first; match tools only work inside a loaded match"}
		}
		if m.GoalPending != nil {
			pending := *m.GoalPending
			if pending.RoundSeq != m.RoundSeq {
				m.GoalPending = nil
				discardedPending = true
				return m, CheckStepJobOutput{Complete: false, Reason: "state_changed", RunID: pending.RunID}, nil
			}
			pollRunGoal = &pending
			return nil, nil, nil
		}
		v := evaluateRound(m)
		if !v.complete {
			s.append("round_adjudicated", map[string]any{"match_id": m.ID, "round": m.Current.Seq, "step": m.Current.StepID, "complete": false, "reason": v.reason})
			return nil, CheckStepJobOutput{Complete: false, Reason: v.reason, OpenTasks: v.open}, nil
		}
		if m.Playbook.Goal != nil && v.outcome == OutcomeSuccess {
			spec := *m.Playbook.Goal
			memoDigest := ""
			if s.goalMemoEnabled() {
				digest, err := s.goalMemoDigest(m, spec)
				if err != nil {
					return nil, nil, err
				}
				memoDigest = digest
				if entry, ok := m.GoalMemo[digest]; ok && entry.Report.Verdict == TaskPass {
					report := memoizedGoalReport(entry)
					s.append("goal_checked", map[string]any{"match_id": m.ID, "round": m.Current.Seq, "verdict": report.Verdict, "memoized": true, "digest": digest})
					next, o := s.settle(m, v, true, report)
					return next, o, nil
				}
			}
			if spec.Kind == "run" {
				if err := s.checkRecipePin(m, spec); err != nil {
					return nil, nil, err
				}
				startRunGoal = &spec
				startRunSeq = m.RoundSeq
				startRunDigest = memoDigest
				return nil, nil, nil
			}
			pendingGoal = &spec
			pendingSeq = m.RoundSeq
			pendingDigest = memoDigest
			return nil, nil, nil // 锁外执行 checkmate 谓词后再落子
		}
		next, o := s.settle(m, v, false, nil)
		return next, o, nil
	})
	if err != nil {
		return CheckStepJobOutput{}, err
	}
	if discardedPending {
		// pending 属于已死回合,刚刚在锁内被清除:goal 生命周期结束,
		// 缓存的 exec 引擎随之关闭(锁已释放,Close 不在文件锁内)。
		s.closeGoalEngine()
	}
	if pollRunGoal != nil {
		return s.pollAsyncRunGoal(ctx, *pollRunGoal)
	}
	if startRunGoal != nil {
		return s.startAsyncRunGoal(ctx, *startRunGoal, startRunSeq, startRunDigest)
	}
	if pendingGoal == nil {
		return out.(CheckStepJobOutput), nil
	}

	result, err := verify.Execute(ctx, s.Root, *pendingGoal)
	if err != nil {
		return CheckStepJobOutput{}, specError(err)
	}
	report := &GoalReport{Verdict: TaskFail, ExitCode: result.ExitCode, IsError: result.IsError, Output: result.Output, DurationMS: result.DurationMS, Failure: result.Failure}
	if verify.Pass(result) {
		report.Verdict = TaskPass
	}

	out, err = s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Status != StatusActive || m.Current == nil || m.RoundSeq != pendingSeq {
			return nil, CheckStepJobOutput{Complete: false, Reason: "state_changed", Goal: report}, nil
		}
		// 冻结测试完整性闸(将死路径):goal 谓词正是宣布胜利的那次裁决。落子前
		// 重算冻结测试哈希,任何改动(经 Bash/git/sed 旁路 guard 后改写、或谓词
		// 副作用)都使 goal 判负 —— 被篡改的测试不能换来将死。与 SubmitTask 同源。
		if violated := frozenViolation(s.Root, m.FrozenTests); violated != "" {
			report = frozenGoalReport(violated)
		}
		if pendingDigest != "" {
			// TOCTOU 防线:goal 执行期间工作区可能已被改写(谓词本身也可能有副作用)。
			// 重算摘要,与执行前一致才记入 memo;否则静默跳过(见 pollAsyncRunGoal)。
			if digest, digestErr := s.goalMemoDigest(m, *pendingGoal); digestErr == nil && digest == pendingDigest {
				rememberGoalMemo(m, pendingDigest, report)
			}
		}
		s.append("goal_checked", map[string]any{"match_id": m.ID, "round": m.Current.Seq, "verdict": report.Verdict, "duration_ms": report.DurationMS, "failure": report.Failure})
		v := evaluateRound(m) // goal 执行期间可能有重交,重算后裁决
		if !v.complete {
			s.append("round_adjudicated", map[string]any{"match_id": m.ID, "round": m.Current.Seq, "step": m.Current.StepID, "complete": false, "reason": v.reason})
			return nil, CheckStepJobOutput{Complete: false, Reason: v.reason, OpenTasks: v.open, Goal: report}, nil
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

// AddPlayBook 校验并注册一份新棋谱(只创建,绝不覆盖)。
func (s *Store) AddPlayBook(content string) (AddPlayBookOutput, error) {
	book, issues := playbook.ParseBytes("(submitted)", []byte(content))
	if len(issues) > 0 {
		return AddPlayBookOutput{}, &ToolError{Code: playbook.CodePlaybookInvalid, Message: "playbook invalid — fix exactly what data.issues lists, then retry", Data: map[string]any{"issues": issues}}
	}
	name := book.Name
	if name != filepath.Base(name) || name == "." || name == ".." {
		return AddPlayBookOutput{}, &ToolError{Code: playbook.CodePlaybookInvalid, Message: "playbook invalid — fix exactly what data.issues lists, then retry", Data: map[string]any{"issues": []playbook.Issue{{Code: playbook.IssueBadFrontmatter, Detail: "name is not a safe file name"}}}}
	}
	out, err := s.withLock(func(m *Match) (*Match, any, error) {
		cat := playbook.ScanDir(s.playbookDir())
		for _, entry := range cat.Entries {
			if entry.Book.Name == name {
				return nil, nil, &ToolError{Code: playbook.CodeNameConflict, Message: "playbook name already exists"}
			}
		}
		path := filepath.Join(s.playbookDir(), name+".md")
		if _, err := os.Stat(path); err == nil {
			return nil, nil, &ToolError{Code: playbook.CodeNameConflict, Message: "playbook file already exists"}
		}
		if err := atomicFile(path, []byte(content), 0o644); err != nil {
			return nil, nil, err
		}
		s.append("playbook_added", map[string]any{"name": name, "file": name + ".md", "bytes": len(content), "steps": len(book.Steps), "has_goal": book.Goal != nil})
		return nil, AddPlayBookOutput{Name: name, File: name + ".md", StepsTotal: len(book.Steps), MaxSteps: book.StepBudget(), HasGoal: book.Goal != nil}, nil
	})
	if err != nil {
		return AddPlayBookOutput{}, err
	}
	return out.(AddPlayBookOutput), nil
}

// StopGate 是宿主 Stop hook 的门控:对局 active 时拒绝模型停止,直至终局或拦截上限。
func (s *Store) StopGate() (StopDecision, error) {
	out, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Status != StatusActive || m.Current == nil {
			return nil, StopDecision{Allow: true}, nil
		}
		m.StopBlocks++
		if m.StopBlocks > playbook.StopBlockCap {
			m.Status = StatusAborted
			m.Abort = AbortStopLimit
			s.append("match_aborted", map[string]any{"match_id": m.ID, "abort": AbortStopLimit, "round": m.Current.Seq})
			return m, StopDecision{Allow: true}, nil
		}
		var open, fail, pass int
		for _, task := range m.Current.Tasks {
			switch task.Status {
			case TaskOpen:
				open++
			case TaskFail:
				fail++
			case TaskPass:
				pass++
			}
		}
		reason := fmt.Sprintf(
			"Arbiter 对局进行中(回合 %d/%d,步骤 %s;任务 %d 待交 / %d 失败 / %d 通过),尚未 checkmate,不能停止。继续规程:ShowStepJob 查看局面 → 创建/重派任务 → CheckStepJob 请求裁决。确需终止请由用户中断。",
			m.Current.Seq, m.Playbook.StepBudget(), m.Current.StepID, open, fail, pass)
		s.append("stop_blocked", map[string]any{"match_id": m.ID, "round": m.Current.Seq, "step": m.Current.StepID, "blocks": m.StopBlocks})
		return m, StopDecision{Allow: false, Reason: reason}, nil
	})
	if err != nil {
		return StopDecision{}, err
	}
	return out.(StopDecision), nil
}

func (s *Store) ReviewTask(taskID string) (ReviewTaskOutput, error) {
	out, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil {
			return nil, nil, &ToolError{Code: playbook.CodeNoMatchLoaded, Message: "no match loaded"}
		}
		if m.Current != nil {
			if idx, ok := findCurrentTask(m, taskID); ok {
				task := m.Current.Tasks[idx]
				return nil, ReviewTaskOutput{TaskID: task.ID, Round: m.Current.Seq, StepID: m.Current.StepID, Archived: false, Status: task.Status, Request: task.Request, Briefing: task.Briefing, Summary: task.Summary, Report: task.Report, Result: task.Result}, nil
			}
		}
		for _, round := range m.History {
			for _, task := range round.Tasks {
				if task.ID == taskID {
					return nil, ReviewTaskOutput{TaskID: task.ID, Round: round.Seq, StepID: round.StepID, Archived: true, Status: task.Status, Request: task.Request, Briefing: task.Briefing, Summary: task.Summary, Report: task.Report, Result: task.Result}, nil
				}
			}
		}
		return nil, nil, &ToolError{Code: playbook.CodeTaskNotFound, Message: "task not found — verify the id against ListTask; ids are match-scoped and case-sensitive"}
	})
	if err != nil {
		return ReviewTaskOutput{}, err
	}
	return out.(ReviewTaskOutput), nil
}

// ListTask 只读返回全部任务(历史回合在前、当前回合在后)的索引:编号/回合/步骤/状态/概要。
func (s *Store) ListTask() (ListTaskOutput, error) {
	out, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil {
			return nil, nil, &ToolError{Code: playbook.CodeNoMatchLoaded, Message: "no match loaded"}
		}
		items := []ListTaskItem{}
		appendRound := func(round Round) {
			for _, task := range round.Tasks {
				items = append(items, ListTaskItem{TaskID: task.ID, Round: round.Seq, StepID: round.StepID, Status: task.Status, Summary: task.Summary})
			}
		}
		for _, round := range m.History {
			appendRound(round)
		}
		if m.Current != nil {
			appendRound(*m.Current)
		}
		return nil, ListTaskOutput{Tasks: items}, nil
	})
	if err != nil {
		return ListTaskOutput{}, err
	}
	return out.(ListTaskOutput), nil
}

// NotePlaybook 把对局中发现的 gotcha 以单行注记追加到棋谱的指定步骤([Gotcha] 节):
// 同时写入棋谱源文件(沉淀给未来对局)与当前对局快照(本局再到该步即随 ShowStepJob 返回)。
// 仅限本局已走过的步骤——棋手可见信息不超出 ShowStepJob ∪ 历史;重复注记幂等跳过。
func (s *Store) NotePlaybook(stepID, note string) (NotePlaybookOutput, error) {
	stepID = strings.TrimSpace(stepID)
	note = strings.TrimSpace(note)
	if note == "" {
		return NotePlaybookOutput{}, &ToolError{Code: playbook.CodeBadNote, Message: "note is empty"}
	}
	if strings.ContainsAny(note, "\r\n") {
		return NotePlaybookOutput{}, &ToolError{Code: playbook.CodeBadNote, Message: "note must be a single line"}
	}
	if len(note) > playbook.MaxNoteBytes {
		return NotePlaybookOutput{}, &ToolError{Code: playbook.CodeBadNote, Message: fmt.Sprintf("note exceeds %d bytes", playbook.MaxNoteBytes)}
	}
	out, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil {
			return nil, nil, &ToolError{Code: playbook.CodeNoMatchLoaded, Message: "no match loaded"}
		}
		if !stepVisited(m, stepID) {
			return nil, nil, &ToolError{Code: playbook.CodeStepNotFound, Message: "step not visited in this match"}
		}
		cat := playbook.ScanDir(s.playbookDir())
		entry, code := cat.Find(m.Playbook.Name)
		if code == playbook.CodePlaybookNotFound {
			return nil, nil, &ToolError{Code: code, Message: "playbook file missing"}
		}
		if code == playbook.CodeNameConflict {
			return nil, nil, &ToolError{Code: code, Message: "playbook name conflict — that intent already has a book; extend it or register under a genuinely different intent name"}
		}
		if code == playbook.CodePlaybookInvalid || len(entry.Problems) > 0 {
			return nil, nil, &ToolError{Code: playbook.CodePlaybookInvalid, Message: "playbook invalid — fix exactly what data.issues lists, then retry", Data: map[string]any{"issues": entry.Problems}}
		}
		path := filepath.Join(s.playbookDir(), entry.File)
		raw, err := os.ReadFile(path)
		if err != nil {
			return nil, nil, &ToolError{Code: playbook.CodePlaybookNotFound, Message: err.Error()}
		}
		book, issues := playbook.ParseBytes(entry.File, raw)
		if len(issues) > 0 {
			return nil, nil, &ToolError{Code: playbook.CodePlaybookInvalid, Message: "playbook invalid — fix exactly what data.issues lists, then retry", Data: map[string]any{"issues": issues}}
		}
		step, ok := book.Steps[stepID]
		if !ok {
			return nil, nil, &ToolError{Code: playbook.CodeStepNotFound, Message: "step missing in playbook file"}
		}

		gotchas := append([]string(nil), step.Gotchas...)
		added := !hasNote(gotchas, note)
		if added {
			next, ok := playbook.AppendGotcha(raw, stepID, note)
			if !ok {
				return nil, nil, &ToolError{Code: playbook.CodeStepNotFound, Message: "step missing in playbook file"}
			}
			if len(next) > playbook.MaxPlaybookBytes {
				return nil, nil, &ToolError{Code: playbook.CodeBadNote, Message: "note would exceed playbook size cap"}
			}
			reparsed, reissues := playbook.ParseBytes(entry.File, next) // 写盘前整体复核,绝不留下坏棋谱
			if len(reissues) > 0 || !hasNote(reparsed.Steps[stepID].Gotchas, note) {
				return nil, nil, &ToolError{Code: playbook.CodeBadNote, Message: "note breaks playbook structure"}
			}
			if err := atomicFile(path, next, 0o644); err != nil {
				return nil, nil, err
			}
			gotchas = append(gotchas, note)
		}
		var dirty *Match
		if snap, ok := m.Playbook.Steps[stepID]; ok && !hasNote(snap.Gotchas, note) {
			snap.Gotchas = append(snap.Gotchas, note)
			m.Playbook.Steps[stepID] = snap
			dirty = m
		}
		s.append("playbook_noted", map[string]any{"match_id": m.ID, "playbook": m.Playbook.Name, "file": entry.File, "step": stepID, "note": note, "added": added})
		return dirty, NotePlaybookOutput{Playbook: m.Playbook.Name, StepID: stepID, Added: added, Gotchas: gotchas}, nil
	})
	if err != nil {
		return NotePlaybookOutput{}, err
	}
	return out.(NotePlaybookOutput), nil
}

func stepVisited(m *Match, stepID string) bool {
	if m.Current != nil && m.Current.StepID == stepID {
		return true
	}
	for _, round := range m.History {
		if round.StepID == stepID {
			return true
		}
	}
	return false
}

func hasNote(notes []string, target string) bool {
	for _, note := range notes {
		if note == target {
			return true
		}
	}
	return false
}

func findCurrentTask(m *Match, taskID string) (int, bool) {
	if m.Current == nil {
		return 0, false
	}
	for i, task := range m.Current.Tasks {
		if task.ID == taskID {
			return i, true
		}
	}
	return 0, false
}

func findHistoryTask(m *Match, taskID string) *Task {
	for _, round := range m.History {
		for i := range round.Tasks {
			if round.Tasks[i].ID == taskID {
				return &round.Tasks[i]
			}
		}
	}
	return nil
}

func specError(err error) error {
	if err == nil {
		return nil
	}
	if e, ok := err.(*verify.SpecError); ok {
		return &ToolError{Code: e.Code, Message: e.Message, Data: e.Data}
	}
	return err
}
