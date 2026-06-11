package playbook

import "encoding/json"

const (
	EndTarget = "END"

	CodePlaybookNotFound     = "playbook_not_found"
	CodePlaybookInvalid      = "playbook_invalid"
	CodeNameConflict         = "name_conflict"
	CodeNoActiveMatch        = "no_active_match"
	CodeNoMatchLoaded        = "no_match_loaded"
	CodeEmptyRequest         = "empty_request"
	CodeBadResult            = "bad_result"
	CodeEngineUnavailable    = "engine_unavailable"
	CodeBriefingUnresolved   = "briefing_unresolved"
	CodeRecipePinMismatch    = "recipe_pin_mismatch"
	CodeServerNotFound       = "server_not_found"
	CodeUnsupportedTransport = "unsupported_transport"
	CodeReservedServer       = "reserved_server"
	CodeTaskNotFound         = "task_not_found"
	CodeTaskStale            = "task_stale"
	CodeStepNotFound         = "step_not_found"
	CodeBadSummary           = "bad_summary"
	CodeBadNote              = "bad_note"
	CodeLockTimeout          = "lock_timeout"
	CodeStateBusy            = "state_busy"
	CodeStateCorrupt         = "state_corrupt"

	IssueBadFrontmatter      = "bad_frontmatter"
	IssueNoSteps             = "no_steps"
	IssueDuplicateStep       = "duplicate_step"
	IssueMissingSection      = "missing_section"
	IssueEmptyJob            = "empty_job"
	IssueEmptyChecklist      = "empty_checklist"
	IssueBadBranch           = "bad_branch"
	IssueUnknownBranchTarget = "unknown_branch_target"
	IssueStrayContent        = "stray_content"
	IssueOversize            = "oversize"
	IssueNameConflict        = "name_conflict"
	IssueBadGoal             = "bad_goal"
	IssueBadVerify           = "bad_verify"
	IssueBadMaxSteps         = "bad_max_steps"

	DefaultTimeoutS    = 600
	MaxTimeoutS        = 3600
	DefaultOutputLines = 256
	MaxOutputLines     = 10000
	MaxOutputBytes     = 1024 * 1024
	LockTimeoutS       = 5
	DefaultMaxSteps    = 256
	MaxStepsCeiling    = 1024
	StopBlockCap       = 32
	MaxPlaybookBytes   = 1024 * 1024
	MaxSummaryBytes    = 1024
	MaxNoteBytes       = 1024

	SeatEnvKey       = "ARBITER_SEAT_KEY"
	SeatKeyHexLength = 32
)

type Playbook struct {
	Name         string                `json:"name"`
	Description  string                `json:"description"`
	Entry        string                `json:"entry"`
	MaxSteps     int                   `json:"max_steps,omitempty"` // 0 = 未配置,生效 DefaultMaxSteps
	Capabilities []string              `json:"capabilities,omitempty"`
	Goal         *ResultSpec           `json:"goal,omitempty"` // checkmate 谓词,可选
	Verify       map[string]ResultSpec `json:"verify,omitempty"`
	Steps        map[string]Step       `json:"steps"`

	order []string
}

// ResultSpec 是验证谓词的共享数据模型(任务 result 与棋谱 goal 共用);
// 本包是依赖图最底层,verify 通过别名复用此类型。
type ResultSpec struct {
	Kind      string         `json:"kind"`                // "shell" | "mcp" | "run" | "fact"
	Command   string         `json:"command,omitempty"`   // shell: /bin/sh -c 执行
	Server    string         `json:"server,omitempty"`    // mcp: .mcp.json 中的服务器名
	Tool      string         `json:"tool,omitempty"`      // mcp: 工具名
	Arguments map[string]any `json:"arguments,omitempty"` // mcp: 工具入参

	Recipe  string         `json:"recipe,omitempty"`  // run: 可选 recipe 名
	Tests   []string       `json:"tests,omitempty"`   // run: 测试目标(必填)
	Options map[string]any `json:"options,omitempty"` // run: 可选执行参数

	Query string `json:"query,omitempty"` // fact: 检索 mini-language(必填)

	// run/fact 的 expect 形状随 kind 不同(对象 vs 子句),原样保留、由 verify 按 kind 严格解析。
	Expect json.RawMessage `json:"expect,omitempty"`

	TimeoutS    int `json:"timeout_s,omitempty"`    // 可选,默认 600,上限 3600
	OutputLines int `json:"output_lines,omitempty"` // 可选,默认 256,上限 10000
}

// StepBudget 返回生效的回合预算。
func (p Playbook) StepBudget() int {
	if p.MaxSteps > 0 {
		return p.MaxSteps
	}
	return DefaultMaxSteps
}

type Step struct {
	ID        string   `json:"id"`
	Job       string   `json:"job"`
	Checklist []string `json:"checklist"`
	Gotchas   []string `json:"gotchas,omitempty"` // [Gotcha] 注记:历史对局沉淀的踩坑提示,可由 NotePlaybook 追加
	Branch    Branch   `json:"branch"`
}

type Branch struct {
	Success string `json:"success"`
	Failure string `json:"failure"`
}

type Issue struct {
	File   string `json:"file,omitempty"`
	Line   int    `json:"line,omitempty"`
	Code   string `json:"code"`
	Detail string `json:"detail,omitempty"`
}

type StepView struct {
	ID        string   `json:"id"`
	Job       string   `json:"job"`
	Checklist []string `json:"checklist"`
	Gotchas   []string `json:"gotchas,omitempty"`
	Branch    Branch   `json:"branch"`
}

type CatalogEntry struct {
	File     string
	Book     Playbook
	Problems []Issue
}

type Catalog struct {
	Entries []CatalogEntry
	Invalid []Issue
}

func (p Playbook) OrderedSteps() []Step {
	out := make([]Step, 0, len(p.order))
	for _, id := range p.order {
		out = append(out, p.Steps[id])
	}
	if len(out) == 0 && len(p.Steps) > 0 {
		for _, step := range p.Steps {
			out = append(out, step)
		}
	}
	return out
}
