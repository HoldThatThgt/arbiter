// Package guard 实现 PreToolUse 门控:任何模型工具调用触碰裁判私有路径
// (棋谱、对局状态、内置引擎、席位 agent 文件)一律拒绝,并返回指路的
// 教学消息。settings 的 Read(...) deny 规则只约束 Read 工具;Bash cat、
// Grep、Glob 全是旁路 —— 本门控是唯一覆盖全部工具面的机制。
// 姿态与 Stop 门控一致:解析失败 fail-open(可用性优先),命中 fail-closed。
package guard

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
)

// Decision 是 PreToolUse 应答:Deny=false 时无输出(放行)。
type Decision struct {
	Deny   bool
	Reason string
}

type zone struct {
	// rel 是仓根相对前缀(斜杠结尾表示目录);match 也接受其绝对形态。
	rel    string
	reason string
}

var zones = []zone{
	{
		rel: ".arbiter/playbook/",
		reason: "Playbooks are referee-owned: reading them directly would reveal future steps and " +
			"unfence the match. Your view of the flow is ShowStepJob (current step only); the curator " +
			"selects via ReadPlayBook; new knowledge goes through AddPlayBook and NotePlaybook.",
	},
	{
		rel: ".arbiter/match/",
		reason: "Match state is referee-owned. Use ShowStepJob / ListTask / ReviewTask / CheckStepJob " +
			"for everything you may know about the match; state files and the journal are not a model surface.",
	},
	{
		rel: ".arbiter/engine/",
		reason: "The embedded engine is the adjudication evaluator, digest-verified on every spawn. " +
			"There is nothing for a model to fix or read here; engine behavior is reached only through seat tools.",
	},
	{
		rel: ".claude/agents/arbiter-",
		reason: "Arbiter seat agent files embed the seat credential and are deploy-generated. " +
			"They are refreshed by `arbiter init`, never edited or read in-session.",
	},
}

// Input 是 Claude Code PreToolUse 事件里本门控关心的字段。
type Input struct {
	ToolName  string          `json:"tool_name"`
	ToolInput json.RawMessage `json:"tool_input"`
}

// frozenReason 是注册测试被改写工具触碰时的拒绝理由(预防层)。注册测试
// 仍可读、可编译、可运行 —— 只是不可改写;Bash 途径的改写交由裁判端的
// 哈希检测兜底(guard 无法在 Bash 命令里区分编译与篡改)。
const frozenReason = "This test is registered and immutable for the rest of the match — no edit, by anyone, by any means. A fix goes in product code; if the test itself is wrong, that is a finding to report (not to edit). The referee also re-hashes every registered test before each verdict, so a modified one can never pass."

// Decide 对一次工具调用做出门控决定。root 必须是绝对仓根。frozen 是当前
// 冻结的测试文件(仓根相对路径),改写类工具触碰即拒。
// 未知工具、未知字段、解析失败一律放行(fail-open)——门控的职责是挡住
// 明确的越界,不是猜测。
func Decide(root string, frozen []string, raw []byte) Decision {
	var input Input
	if err := json.Unmarshal(raw, &input); err != nil {
		return Decision{}
	}
	if len(input.ToolInput) == 0 {
		return Decision{}
	}
	var fields map[string]any
	if err := json.Unmarshal(input.ToolInput, &fields); err != nil {
		return Decision{}
	}
	switch input.ToolName {
	case "Bash":
		// 冻结测试不在 Bash 这里拦:编译/运行测试是正当且必需的,无法与
		// 篡改区分;Bash 改写由 SubmitTask 的哈希检测层判负兜底。
		command, _ := fields["command"].(string)
		return decideText(root, command)
	case "Read":
		// 注册测试可读(implementer 需要看断言);只过裁判私有静态区。
		return decidePath(root, filePath(fields))
	case "Edit", "Write", "NotebookEdit":
		path := filePath(fields)
		if decision := decideFrozen(root, frozen, path); decision.Deny {
			return decision
		}
		return decidePath(root, path)
	case "Glob", "Grep":
		decision := decidePath(root, stringField(fields, "path"))
		if decision.Deny {
			return decision
		}
		return decideText(root, stringField(fields, "pattern"))
	default:
		return Decision{}
	}
}

func filePath(fields map[string]any) string {
	path, _ := fields["file_path"].(string)
	if path == "" {
		path, _ = fields["notebook_path"].(string)
	}
	return path
}

// decideFrozen 判定改写类工具是否触碰某个冻结测试文件。三道比对:解析后绝对
// 路径词法精确比对(覆盖文件尚不存在的 Write);文件身份(os.SameFile,即
// inode/设备)比对,挡住大小写不敏感卷上的大小写变体、以及指向冻结测试的软链
// 别名;若候选是悬挂软链(指向已被删除的冻结测试,os.Stat 跟随失败),按其
// readlink 目标的词法路径兜一道。命中任一即拒;三道都只增不减拒绝。
func decideFrozen(root string, frozen []string, path string) Decision {
	if path == "" || len(frozen) == 0 {
		return Decision{}
	}
	resolved := path
	if !filepath.IsAbs(resolved) {
		resolved = filepath.Join(root, resolved)
	}
	resolved = filepath.Clean(resolved)
	candInfo, candErr := os.Stat(resolved) // 跟随符号链接;文件不存在时 candErr != nil
	// 悬挂软链兜底:os.Stat 跟随失败时,取其链接目标的词法绝对路径再比对一次。
	linkTarget := ""
	if candErr != nil {
		if linfo, lerr := os.Lstat(resolved); lerr == nil && linfo.Mode()&os.ModeSymlink != 0 {
			if t, rerr := os.Readlink(resolved); rerr == nil {
				if !filepath.IsAbs(t) {
					t = filepath.Join(filepath.Dir(resolved), t)
				}
				linkTarget = filepath.Clean(t)
			}
		}
	}
	for _, rel := range frozen {
		frozenAbs := filepath.Clean(filepath.Join(root, filepath.FromSlash(rel)))
		if resolved == frozenAbs || (linkTarget != "" && linkTarget == frozenAbs) {
			return Decision{Deny: true, Reason: frozenReason}
		}
		if candErr == nil {
			if frozenInfo, err := os.Stat(frozenAbs); err == nil && os.SameFile(candInfo, frozenInfo) {
				return Decision{Deny: true, Reason: frozenReason}
			}
		}
	}
	return Decision{}
}

func stringField(fields map[string]any, key string) string {
	value, _ := fields[key].(string)
	return value
}

// decidePath 判定单个路径:相对路径按仓根解析;绝对路径按前缀比对。
func decidePath(root, path string) Decision {
	if path == "" {
		return Decision{}
	}
	resolved := path
	if !filepath.IsAbs(resolved) {
		resolved = filepath.Join(root, resolved)
	}
	resolved = filepath.Clean(resolved)
	for _, z := range zones {
		absZone := filepath.Join(root, filepath.FromSlash(strings.TrimSuffix(z.rel, "/")))
		if strings.HasPrefix(resolved, absZone) {
			return Decision{Deny: true, Reason: z.reason}
		}
	}
	return Decision{}
}

// decideText 在自由文本(Bash 命令、glob/grep pattern)里扫描受护路径的
// 字面出现 —— 相对与绝对两种写法都算。宁可误杀(消息会指明正道),
// 不可漏放。
func decideText(root, text string) Decision {
	if text == "" {
		return Decision{}
	}
	for _, z := range zones {
		token := strings.TrimSuffix(z.rel, "/")
		if strings.Contains(text, token) {
			return Decision{Deny: true, Reason: z.reason}
		}
		if strings.Contains(text, filepath.Join(root, filepath.FromSlash(token))) {
			return Decision{Deny: true, Reason: z.reason}
		}
	}
	return Decision{}
}
