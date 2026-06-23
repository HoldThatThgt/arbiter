package playbook

import (
	"encoding/json"
	"sort"
)

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
	CodeCapabilityRevoked    = "capability_revoked"
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
	CodeVerifyNotFound       = "verify_not_found"
	CodeVerifyPolicy         = "verify_policy"
	CodeVerifyOverride       = "verify_override"
	CodeStepSubmitMismatch   = "step_submit_mismatch"
	CodeTestRegister         = "test_register"
	CodeFrozenTestModified   = "frozen_test_modified"
	CodeCheckpoint           = "checkpoint"

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
	IssueBadSubmit           = "bad_submit"
	IssueBadCheckpoint       = "bad_checkpoint"

	DefaultTimeoutS    = 600
	MaxTimeoutS        = 3600
	DefaultOutputLines = 256
	MaxOutputLines     = 10000
	MaxOutputBytes     = 1024 * 1024
	LockTimeoutS       = 5
	DefaultMaxSteps    = 256
	MaxStepsCeiling    = 1024
	StopBlockCap       = 32
	// SubagentBlockCap 是单个 task 上子代理停止可被拦截的次数上限;到顶
	// 放行,把重派决定交还给 player(镜像 StopBlockCap 的放行姿态)。
	SubagentBlockCap = 8
	MaxPlaybookBytes = 1024 * 1024
	MaxSummaryBytes  = 1024
	MaxNoteBytes     = 1024

	SeatEnvKey       = "ARBITER_SEAT_KEY"
	SeatKeyHexLength = 32
)

type Playbook struct {
	Name         string                `json:"name"`
	Description  string                `json:"description"`
	Entry        string                `json:"entry"`
	MaxSteps     int                   `json:"max_steps,omitempty"` // 0 = 未配置,生效 DefaultMaxSteps
	Capabilities []string              `json:"capabilities,omitempty"`
	VerifyPolicy string                `json:"verify_policy,omitempty"` // "" / "open"(默认)| "named":SubmitTask 只接受具名 [Verify] 引用
	Goal         *ResultSpec           `json:"goal,omitempty"`          // checkmate 谓词,可选
	Verify       map[string]ResultSpec `json:"verify,omitempty"`
	Steps        map[string]Step       `json:"steps"`

	order []string
}

// ResultSpec 是验证谓词的共享数据模型(任务 result 与棋谱 goal 共用);
// 本包是依赖图最底层,verify 通过别名复用此类型。
type ResultSpec struct {
	// Verify 引用棋谱里的具名 [Verify] 谓词,与一切内联 kind 键互斥;
	// 由 match 在锁内对照对局快照解析成 curated spec 后才会执行。
	Verify string `json:"verify,omitempty"`

	Kind      string         `json:"kind"`                // "shell" | "mcp" | "run" | "fact"
	Command   string         `json:"command,omitempty"`   // shell: /bin/sh -c 执行
	Server    string         `json:"server,omitempty"`    // mcp: .mcp.json 中的服务器名
	Tool      string         `json:"tool,omitempty"`      // mcp: 工具名
	Arguments map[string]any `json:"arguments,omitempty"` // mcp: 工具入参

	Recipe  string         `json:"recipe,omitempty"`  // run: recipe 名(必填)
	Tests   []string       `json:"tests,omitempty"`   // run: 测试目标(必填)
	Options map[string]any `json:"options,omitempty"` // run: 可选执行参数

	Query string `json:"query,omitempty"` // fact: 检索 mini-language(必填)

	// run/fact 的 expect 形状随 kind 不同(对象 vs 子句),原样保留、由 verify 按 kind 严格解析。
	Expect json.RawMessage `json:"expect,omitempty"`

	TimeoutS    int `json:"timeout_s,omitempty"`    // 可选,默认 600,上限 3600
	OutputLines int `json:"output_lines,omitempty"` // 可选,默认 256,上限 10000

	// AllowOverrides 仅限 curated [Verify] 谓词:声明提交侧可随 verify 引用一起
	// 覆盖的字段,合法值只有 "tests" 与 "options"(expect/kind/recipe/command 永不可覆盖)。
	AllowOverrides []string `json:"allow_overrides,omitempty"`
}

// Clone 返回 spec 的深拷贝(map/slice/RawMessage 全部独立底层存储),
// 供对局快照与 goal 别名解析使用,杜绝共享存储被事后改写。
func (s ResultSpec) Clone() ResultSpec {
	out := s
	out.Arguments = cloneAnyMap(s.Arguments)
	out.Tests = append([]string(nil), s.Tests...)
	out.Options = cloneAnyMap(s.Options)
	out.Expect = append(json.RawMessage(nil), s.Expect...)
	out.AllowOverrides = append([]string(nil), s.AllowOverrides...)
	return out
}

func cloneAnyMap(in map[string]any) map[string]any {
	if in == nil {
		return nil
	}
	out := make(map[string]any, len(in))
	for key, value := range in {
		out[key] = value
	}
	return out
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
	// Submit 是该步骤强绑定的具名 [Verify] 谓词:本步骤的任务提交必须引用它
	// (允许其 allow_overrides 范围内的覆盖),裁判拒绝任何其他谓词或内联 spec。
	// 空 = 不绑定(沿用 verify_policy 的全局规则)。这把"用哪条证据"从模型手里
	// 收归棋谱:模型连选哪条 curated 谓词的自由都没有,只能填 allow_overrides。
	Submit string `json:"submit,omitempty"`
	// Checkpoint 非空时本步骤是"人工确认关卡":没有可执行谓词,由用户对这段
	// 问题文本作 pass/fail 决定裁决(player 弹 AskUserQuestion 取得,经
	// SubmitCheckpoint 回传)。互斥于 Checklist —— 关卡步骤无执行任务。
	Checkpoint string `json:"checkpoint,omitempty"`
	Branch     Branch `json:"branch"`
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
	ID         string   `json:"id"`
	Job        string   `json:"job"`
	Checklist  []string `json:"checklist"`
	Gotchas    []string `json:"gotchas,omitempty"`
	Submit     string   `json:"submit,omitempty"`
	Checkpoint string   `json:"checkpoint,omitempty"`
	Branch     Branch   `json:"branch"`
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
	// order 不带 JSON tag,从对局状态反序列化的 Playbook 会丢失它;此时退回
	// 按 step id 排序遍历,避免裸 map 遍历带来的不确定顺序。
	if len(out) == 0 && len(p.Steps) > 0 {
		ids := make([]string, 0, len(p.Steps))
		for id := range p.Steps {
			ids = append(ids, id)
		}
		sort.Strings(ids)
		for _, id := range ids {
			out = append(out, p.Steps[id])
		}
	}
	return out
}
