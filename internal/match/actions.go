package match

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
	"github.com/HoldThatThgt/arbiter/internal/verify"
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

func (s *Store) LoadPlayBook(name string) (LoadPlayBookOutput, error) {
	cat := playbook.ScanDir(s.playbookDir())
	entry, code := cat.Find(name)
	if code == playbook.CodePlaybookNotFound {
		return LoadPlayBookOutput{}, &ToolError{Code: code, Message: "playbook not found", Data: map[string]any{"available": cat.LoadableNames()}}
	}
	if code == playbook.CodeNameConflict {
		return LoadPlayBookOutput{}, &ToolError{Code: code, Message: "playbook name conflict", Data: map[string]any{"name": name}}
	}
	if code == playbook.CodePlaybookInvalid {
		return LoadPlayBookOutput{}, &ToolError{Code: code, Message: "playbook invalid", Data: map[string]any{"issues": entry.Problems}}
	}
	if len(entry.Problems) > 0 {
		return LoadPlayBookOutput{}, &ToolError{Code: playbook.CodePlaybookInvalid, Message: "playbook invalid", Data: map[string]any{"issues": entry.Problems}}
	}
	recipesPin, err := s.currentRecipesPin()
	if err != nil {
		return LoadPlayBookOutput{}, &ToolError{Code: playbook.CodeRecipePinMismatch, Message: "recipe pin mismatch", Data: map[string]any{"error": err.Error()}}
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
			Status:     StatusActive,
			Current:    &Round{Seq: 1, StepID: entry.Book.Entry, EnteredAt: now.Format(time.RFC3339)},
			History:    []Round{},
			RoundSeq:   1,
			StartedAt:  now.Format(time.RFC3339),
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
			Step:     &StepOutput{ID: step.ID, Job: step.Job, Checklist: step.Checklist, Gotchas: step.Gotchas},
			Tasks:    tasks,
		}, nil
	})
	if err != nil {
		return ShowStepJobOutput{}, err
	}
	return out.(ShowStepJobOutput), nil
}

func (s *Store) CreateTask(request string) (CreateTaskOutput, error) {
	request = strings.TrimSpace(request)
	if request == "" {
		return CreateTaskOutput{}, &ToolError{Code: playbook.CodeEmptyRequest, Message: "request is empty"}
	}
	out, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Status != StatusActive || m.Current == nil {
			return nil, nil, &ToolError{Code: playbook.CodeNoActiveMatch, Message: "no active match"}
		}
		m.TaskSeq++
		task := Task{ID: fmt.Sprintf("T%d", m.TaskSeq), Request: request, Status: TaskOpen}
		m.Current.Tasks = append(m.Current.Tasks, task)
		s.append("task_created", map[string]any{"match_id": m.ID, "task": task.ID, "request": task.Request})
		return m, CreateTaskOutput{TaskID: task.ID, StepID: m.Current.StepID}, nil
	})
	if err != nil {
		return CreateTaskOutput{}, err
	}
	return out.(CreateTaskOutput), nil
}

func (s *Store) SubmitTask(ctx context.Context, taskID, summary, report string, spec verify.ResultSpec) (SubmitTaskOutput, error) {
	summary = strings.TrimSpace(summary)
	if summary == "" {
		return SubmitTaskOutput{}, &ToolError{Code: playbook.CodeBadSummary, Message: "summary is empty"}
	}
	if len(summary) > playbook.MaxSummaryBytes {
		return SubmitTaskOutput{}, &ToolError{Code: playbook.CodeBadSummary, Message: fmt.Sprintf("summary exceeds %d bytes", playbook.MaxSummaryBytes)}
	}
	var roundSeq int
	var matchID string
	out, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Status != StatusActive || m.Current == nil {
			return nil, nil, &ToolError{Code: playbook.CodeNoActiveMatch, Message: "no active match"}
		}
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
			return nil, nil, nil
		}
		if findHistoryTask(m, taskID) != nil {
			return nil, nil, &ToolError{Code: playbook.CodeTaskStale, Message: "task belongs to archived round"}
		}
		return nil, nil, &ToolError{Code: playbook.CodeTaskNotFound, Message: "task not found"}
	})
	if err != nil {
		return SubmitTaskOutput{}, err
	}
	_ = out

	result, err := verify.ExecuteWithMeta(ctx, s.Root, spec, map[string]any{"match_id": matchID, "round_seq": roundSeq})
	if err != nil {
		return SubmitTaskOutput{}, specError(err)
	}
	verdict := TaskFail
	if verify.Pass(result) {
		verdict = TaskPass
	}

	out, err = s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Status != StatusActive || m.Current == nil {
			return nil, nil, &ToolError{Code: playbook.CodeTaskStale, Message: "task belongs to archived round"}
		}
		if m.RoundSeq != roundSeq {
			s.append("task_submitted", map[string]any{"match_id": m.ID, "task": taskID, "verdict": "stale"})
			return nil, nil, &ToolError{Code: playbook.CodeTaskStale, Message: "task belongs to archived round"}
		}
		idx, ok := findCurrentTask(m, taskID)
		if !ok {
			return nil, nil, &ToolError{Code: playbook.CodeTaskStale, Message: "task belongs to archived round"}
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
		if result.ExitCode != nil {
			fields["exit_code"] = *result.ExitCode
		}
		if result.IsError != nil {
			fields["is_error"] = *result.IsError
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
	m.Current = &Round{Seq: nextSeq, StepID: v.target, EnteredAt: utcNow()}
	s.append("round_entered", map[string]any{"match_id": m.ID, "round": nextSeq, "step": v.target})
	out.NextStep = v.target
	out.Round = nextSeq
	return m, out
}

func (s *Store) CheckStepJob(ctx context.Context) (CheckStepJobOutput, error) {
	var pendingGoal *playbook.ResultSpec
	var pendingSeq int
	var startRunGoal *playbook.ResultSpec
	var startRunSeq int
	var pollRunGoal *GoalPending
	out, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Status != StatusActive || m.Current == nil {
			return nil, nil, &ToolError{Code: playbook.CodeNoActiveMatch, Message: "no active match"}
		}
		if m.GoalPending != nil {
			pending := *m.GoalPending
			if pending.RoundSeq != m.RoundSeq {
				m.GoalPending = nil
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
			if spec.Kind == "run" {
				if err := s.checkRecipePin(m, spec); err != nil {
					return nil, nil, err
				}
				startRunGoal = &spec
				startRunSeq = m.RoundSeq
				return nil, nil, nil
			}
			pendingGoal = &spec
			pendingSeq = m.RoundSeq
			return nil, nil, nil // 锁外执行 checkmate 谓词后再落子
		}
		next, o := s.settle(m, v, false, nil)
		return next, o, nil
	})
	if err != nil {
		return CheckStepJobOutput{}, err
	}
	if pollRunGoal != nil {
		return s.pollAsyncRunGoal(ctx, *pollRunGoal)
	}
	if startRunGoal != nil {
		return s.startAsyncRunGoal(ctx, *startRunGoal, startRunSeq)
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
		return AddPlayBookOutput{}, &ToolError{Code: playbook.CodePlaybookInvalid, Message: "playbook invalid", Data: map[string]any{"issues": issues}}
	}
	name := book.Name
	if name != filepath.Base(name) || name == "." || name == ".." {
		return AddPlayBookOutput{}, &ToolError{Code: playbook.CodePlaybookInvalid, Message: "playbook invalid", Data: map[string]any{"issues": []playbook.Issue{{Code: playbook.IssueBadFrontmatter, Detail: "name is not a safe file name"}}}}
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
				return nil, ReviewTaskOutput{TaskID: task.ID, Round: m.Current.Seq, StepID: m.Current.StepID, Archived: false, Status: task.Status, Request: task.Request, Summary: task.Summary, Report: task.Report, Result: task.Result}, nil
			}
		}
		for _, round := range m.History {
			for _, task := range round.Tasks {
				if task.ID == taskID {
					return nil, ReviewTaskOutput{TaskID: task.ID, Round: round.Seq, StepID: round.StepID, Archived: true, Status: task.Status, Request: task.Request, Summary: task.Summary, Report: task.Report, Result: task.Result}, nil
				}
			}
		}
		return nil, nil, &ToolError{Code: playbook.CodeTaskNotFound, Message: "task not found"}
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
			return nil, nil, &ToolError{Code: code, Message: "playbook name conflict"}
		}
		if code == playbook.CodePlaybookInvalid || len(entry.Problems) > 0 {
			return nil, nil, &ToolError{Code: playbook.CodePlaybookInvalid, Message: "playbook invalid", Data: map[string]any{"issues": entry.Problems}}
		}
		path := filepath.Join(s.playbookDir(), entry.File)
		raw, err := os.ReadFile(path)
		if err != nil {
			return nil, nil, &ToolError{Code: playbook.CodePlaybookNotFound, Message: err.Error()}
		}
		book, issues := playbook.ParseBytes(entry.File, raw)
		if len(issues) > 0 {
			return nil, nil, &ToolError{Code: playbook.CodePlaybookInvalid, Message: "playbook invalid", Data: map[string]any{"issues": issues}}
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
