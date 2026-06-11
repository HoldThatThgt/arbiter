package match

import (
	"github.com/HoldThatThgt/arbiter/internal/playbook"
	"github.com/HoldThatThgt/arbiter/internal/verify"
)

const (
	StatusActive          = "active"
	StatusFinishedSuccess = "finished_success"
	StatusFinishedFailure = "finished_failure"
	StatusAborted         = "aborted"

	AbortStepsExhausted = "steps_exhausted"
	AbortStopLimit      = "stop_limit"

	TaskOpen = "open"
	TaskPass = "pass"
	TaskFail = "fail"

	OutcomeSuccess = "success"
	OutcomeFailure = "failure"
)

type Match struct {
	ID         string            `json:"id"`
	Playbook   playbook.Playbook `json:"playbook"`
	RecipesPin RecipePin         `json:"recipes_pin,omitempty"`
	// VerifyPolicy/VerifySpecs 是装载时从棋谱封盘的快照(镜像 RecipePin 信任模型):
	// 对局中改写棋谱文件换不掉任何在局谓词;SubmitTask 的 verify 引用只对照这里解析。
	// 旧 state.json 两字段缺省 → 策略 open、无具名谓词,行为与历史一致。
	VerifyPolicy string                       `json:"verify_policy,omitempty"`
	VerifySpecs  map[string]verify.ResultSpec `json:"verify_specs,omitempty"`
	Status       string                       `json:"status"`
	Abort        string                       `json:"abort,omitempty"`
	Current      *Round                       `json:"current,omitempty"`
	History      []Round                      `json:"history"`
	TaskSeq      int                          `json:"task_seq"`
	RoundSeq     int                          `json:"round_seq"`
	StopBlocks   int                          `json:"stop_blocks"` // 本回合内被拦截的停止次数,进入新回合清零
	GoalPending  *GoalPending                 `json:"goal_pending,omitempty"`
	GoalMemo     map[string]GoalMemoEntry     `json:"goal_memo,omitempty"`
	StartedAt    string                       `json:"started_at"`
}

type GoalPending struct {
	RoundSeq   int                 `json:"round_seq"`
	RunID      string              `json:"run_id"`
	Spec       playbook.ResultSpec `json:"spec"`
	MemoDigest string              `json:"memo_digest,omitempty"`
	StartedAt  string              `json:"started_at"`
}

type GoalMemoEntry struct {
	Report   GoalReport `json:"report"`
	StoredAt string     `json:"stored_at"`
}

type RecipePin struct {
	BookSHA256 string `json:"book_sha256"`
	// Targets 只承载目标 id 的集合(新写入的值恒为空串)。保留 map[string]string
	// 是为了让旧 state.json(值是逐目标哈希)继续可加载;比较只看键的存在性,
	// 因为 BookSHA256 相等已保证两本书逐字节一致(见 checkRecipePin)。
	Targets map[string]string `json:"targets"`
}

type Round struct {
	Seq       int    `json:"seq"`
	StepID    string `json:"step_id"`
	Tasks     []Task `json:"tasks"`
	Outcome   string `json:"outcome,omitempty"`
	EnteredAt string `json:"entered_at"`
}

type Task struct {
	ID       string         `json:"id"`
	Request  string         `json:"request"`
	Status   string         `json:"status"`
	Briefing []BriefingCard `json:"briefing,omitempty"`
	Summary  string         `json:"summary,omitempty"` // executor 提交的一句话结果概要
	Report   string         `json:"report,omitempty"`
	Result   *verify.Result `json:"result,omitempty"`
}

type BriefingCard struct {
	Ref     string `json:"ref"`
	Content string `json:"content"`
}

type ToolError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
	Data    any    `json:"data,omitempty"`
}

func (e *ToolError) Error() string {
	return e.Message
}

type TaskSummary struct {
	ID      string `json:"id"`
	Status  string `json:"status"`
	Request string `json:"request"`
	Summary string `json:"summary,omitempty"`
}

type ShowStepJobOutput struct {
	Status   string        `json:"status"`
	Hint     string        `json:"hint,omitempty"`
	Playbook string        `json:"playbook,omitempty"`
	Round    int           `json:"round,omitempty"`
	Rounds   int           `json:"rounds,omitempty"`
	Abort    string        `json:"abort,omitempty"`
	Step     *StepOutput   `json:"step,omitempty"`
	Tasks    []TaskSummary `json:"tasks,omitempty"`
	Verify   []VerifyDecl  `json:"verify,omitempty"` // 对局快照里可用的具名谓词(按名排序)
}

// VerifyDecl 是具名 [Verify] 谓词的路由摘要:执行席按名提交,内容由裁判解析。
type VerifyDecl struct {
	Name string `json:"name"`
	Kind string `json:"kind"`
}

type StepOutput struct {
	ID        string   `json:"id"`
	Job       string   `json:"job"`
	Checklist []string `json:"checklist"`
	Gotchas   []string `json:"gotchas,omitempty"`
}

type CreateTaskOutput struct {
	TaskID string `json:"task_id"`
	StepID string `json:"step_id"`
}

type SubmitTaskOutput struct {
	TaskID     string `json:"task_id"`
	Verdict    string `json:"verdict"`
	ExitCode   *int   `json:"exit_code,omitempty"`
	IsError    *bool  `json:"is_error,omitempty"`
	Output     string `json:"output"`
	DurationMS int    `json:"duration_ms"`
	Failure    string `json:"failure,omitempty"`
}

type CheckStepJobOutput struct {
	Complete  bool        `json:"complete"`
	Reason    string      `json:"reason,omitempty"`
	OpenTasks []string    `json:"open_tasks,omitempty"`
	Outcome   string      `json:"outcome,omitempty"`
	NextStep  string      `json:"next_step,omitempty"`
	Round     int         `json:"round,omitempty"`
	Match     string      `json:"match,omitempty"`
	Abort     string      `json:"abort,omitempty"`
	Checkmate bool        `json:"checkmate,omitempty"` // goal 谓词通过,直接胜局
	Goal      *GoalReport `json:"goal,omitempty"`      // 本次裁决执行过 goal 时附带
	RunID     string      `json:"run_id,omitempty"`
}

// GoalReport 是 checkmate 谓词的一次执行结局。
type GoalReport struct {
	Verdict    string `json:"verdict"` // pass | fail
	RunID      string `json:"run_id,omitempty"`
	ExitCode   *int   `json:"exit_code,omitempty"`
	IsError    *bool  `json:"is_error,omitempty"`
	Output     string `json:"output"`
	DurationMS int    `json:"duration_ms"`
	Failure    string `json:"failure,omitempty"`
	Memoized   bool   `json:"memoized,omitempty"`
}

type AddPlayBookOutput struct {
	Name       string `json:"name"`
	File       string `json:"file"`
	StepsTotal int    `json:"steps_total"`
	MaxSteps   int    `json:"max_steps"`
	HasGoal    bool   `json:"has_goal"`
}

// StopDecision 是停止门控的裁定:Allow=false 时 Reason 会作为继续工作的指引返回给模型。
type StopDecision struct {
	Allow  bool   `json:"allow"`
	Reason string `json:"reason,omitempty"`
}

type ReviewTaskOutput struct {
	TaskID   string         `json:"task_id"`
	Round    int            `json:"round"`
	StepID   string         `json:"step_id"`
	Archived bool           `json:"archived"`
	Status   string         `json:"status"`
	Request  string         `json:"request"`
	Briefing []BriefingCard `json:"briefing,omitempty"`
	Summary  string         `json:"summary,omitempty"`
	Report   string         `json:"report,omitempty"`
	Result   *verify.Result `json:"result,omitempty"`
}

// ListTaskItem 是任务索引的一行:编号与一句话概要,细节走 ReviewTask。
type ListTaskItem struct {
	TaskID  string `json:"task_id"`
	Round   int    `json:"round"`
	StepID  string `json:"step_id"`
	Status  string `json:"status"`
	Summary string `json:"summary,omitempty"`
}

type ListTaskOutput struct {
	Tasks []ListTaskItem `json:"tasks"`
}

type NotePlaybookOutput struct {
	Playbook string   `json:"playbook"`
	StepID   string   `json:"step_id"`
	Added    bool     `json:"added"` // false = 该步骤已有相同注记,未重复写入
	Gotchas  []string `json:"gotchas"`
}

type LoadPlayBookOutput struct {
	MatchID       string  `json:"match_id"`
	Playbook      string  `json:"playbook"`
	FirstStep     string  `json:"first_step"`
	StepsTotal    int     `json:"steps_total"`
	ReplacedMatch *string `json:"replaced_match"`
}
