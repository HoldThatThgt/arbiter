package match

import "github.com/HoldThatThgt/arbiter/internal/playbook"

// SubmitCheckpointOutput 回报本次关卡裁定。
type SubmitCheckpointOutput struct {
	StepID   string `json:"step_id"`
	Decision string `json:"decision"`
	Round    int    `json:"round"`
}

// SubmitCheckpoint 记录用户对当前人工确认关卡([Checkpoint] 步骤)的决定。
// decision 仅接受结构化枚举 "pass" | "fail"(用户从 AskUserQuestion 的选择,
// 由 player 原样回传——不解析自由文本)。仅当前步骤确为关卡时有效;裁决在随后
// 的 CheckStepJob(evaluateRound)按此决定走分支:pass→success,fail→failure。
func (s *Store) SubmitCheckpoint(decision string) (SubmitCheckpointOutput, error) {
	if decision != TaskPass && decision != TaskFail {
		return SubmitCheckpointOutput{}, &ToolError{Code: playbook.CodeCheckpoint, Message: "decision must be \"pass\" or \"fail\" — relay exactly the user's choice from the checkpoint question, never your own judgement"}
	}
	out, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Status != StatusActive || m.Current == nil {
			return nil, nil, &ToolError{Code: playbook.CodeNoActiveMatch, Message: "no active match — the curator must LoadPlayBook first"}
		}
		if m.Playbook.Steps[m.Current.StepID].Checkpoint == "" {
			return nil, nil, &ToolError{Code: playbook.CodeCheckpoint, Message: "the current step is not a [Checkpoint] — task steps are adjudicated by SubmitTask, not SubmitCheckpoint"}
		}
		m.Current.Checkpoint = decision
		s.append("checkpoint_submitted", map[string]any{"match_id": m.ID, "round": m.Current.Seq, "step": m.Current.StepID, "decision": decision})
		return m, SubmitCheckpointOutput{StepID: m.Current.StepID, Decision: decision, Round: m.Current.Seq}, nil
	})
	if err != nil {
		return SubmitCheckpointOutput{}, err
	}
	return out.(SubmitCheckpointOutput), nil
}
