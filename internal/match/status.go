package match

import "time"

type statusView struct {
	MatchID     string          `json:"match_id,omitempty"`
	Playbook    string          `json:"playbook,omitempty"`
	Status      string          `json:"status"`
	Abort       string          `json:"abort,omitempty"`
	Round       int             `json:"round,omitempty"`
	CurrentStep *currentStep    `json:"current_step,omitempty"`
	History     []historyStatus `json:"history"`
	UpdatedAt   string          `json:"updated_at"`
}

type currentStep struct {
	ID        string       `json:"id"`
	Job       string       `json:"job"`
	Checklist []string     `json:"checklist"`
	Gotchas   []string     `json:"gotchas,omitempty"`
	Tasks     []taskStatus `json:"tasks"`
}

type taskStatus struct {
	ID      string `json:"id"`
	Status  string `json:"status"`
	Request string `json:"request"`
	Summary string `json:"summary,omitempty"`
}

type historyStatus struct {
	Round   int    `json:"round"`
	Step    string `json:"step"`
	Outcome string `json:"outcome"`
	Tasks   int    `json:"tasks"`
}

func projectStatus(m *Match) statusView {
	view := statusView{
		MatchID:   m.ID,
		Playbook:  m.Playbook.Name,
		Status:    m.Status,
		Abort:     m.Abort,
		History:   make([]historyStatus, 0, len(m.History)),
		UpdatedAt: time.Now().UTC().Format(time.RFC3339),
	}
	for _, round := range m.History {
		view.History = append(view.History, historyStatus{
			Round:   round.Seq,
			Step:    round.StepID,
			Outcome: round.Outcome,
			Tasks:   len(round.Tasks),
		})
	}
	if m.Current != nil {
		step := m.Playbook.Steps[m.Current.StepID]
		view.Round = m.Current.Seq
		view.CurrentStep = &currentStep{
			ID:        step.ID,
			Job:       step.Job,
			Checklist: append([]string(nil), step.Checklist...),
			Gotchas:   append([]string(nil), step.Gotchas...),
			Tasks:     make([]taskStatus, 0, len(m.Current.Tasks)),
		}
		for _, task := range m.Current.Tasks {
			view.CurrentStep.Tasks = append(view.CurrentStep.Tasks, taskStatus{
				ID:      task.ID,
				Status:  task.Status,
				Request: task.Request,
				Summary: task.Summary,
			})
		}
	}
	return view
}
