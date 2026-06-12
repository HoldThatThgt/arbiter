package match

import (
	"sort"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
	"github.com/HoldThatThgt/arbiter/internal/verify"
)

// 本文件实现具名 [Verify] 谓词的对局接线(docs/proposals/verify-predicates-and-comments.md
// Part 1):装载时封盘快照、提交时锁内解析、allow_overrides 受控开口。

// cloneVerifySpecs 深拷贝棋谱的具名谓词表进对局快照:装载后改写棋谱文件
// 换不掉任何在局谓词(与 RecipePin 同一信任模型)。
func cloneVerifySpecs(specs map[string]playbook.ResultSpec) map[string]verify.ResultSpec {
	if len(specs) == 0 {
		return nil
	}
	out := make(map[string]verify.ResultSpec, len(specs))
	for name, spec := range specs {
		out[name] = spec.Clone()
	}
	return out
}

// inlineVerifyField 返回与 verify 引用同时被设置的内联字段名;tests/options
// 不在此列(它们走 allow_overrides 通道,由 resolveVerifySpec 按声明放行)。
func inlineVerifyField(spec verify.ResultSpec) string {
	switch {
	case spec.Kind != "":
		return "kind"
	case spec.Command != "":
		return "command"
	case spec.Server != "":
		return "server"
	case spec.Tool != "":
		return "tool"
	case len(spec.Arguments) != 0:
		return "arguments"
	case spec.Recipe != "":
		return "recipe"
	case spec.Query != "":
		return "query"
	case len(spec.Expect) != 0:
		return "expect"
	case spec.TimeoutS != 0:
		return "timeout_s"
	case spec.OutputLines != 0:
		return "output_lines"
	}
	return ""
}

// resolveVerifySpec 在锁内把提交的 spec 解析为可执行谓词:
//   - verify 引用 → 对照对局快照取 curated spec,按其 allow_overrides 套用 tests/options;
//   - 无引用且 verify_policy=named → 拒绝内联谓词(裁决必须出自 curated 谓词);
//   - allow_overrides 是 curator 专属声明,提交侧出现即拒绝。
//
// 返回值:解析后的 spec(随后照常流经 Validate → recipe pin → ExecuteWithMeta)、
// 引用名(供 journal 记账,内联提交为空)、错误。
func resolveVerifySpec(m *Match, spec verify.ResultSpec) (verify.ResultSpec, string, error) {
	if len(spec.AllowOverrides) != 0 {
		return spec, "", &ToolError{Code: playbook.CodeVerifyOverride, Message: "allow_overrides is declared by the playbook, not by submissions"}
	}
	if spec.Verify == "" {
		if m.VerifyPolicy == "named" {
			return spec, "", &ToolError{Code: playbook.CodeVerifyPolicy, Message: "this playbook requires a named [Verify] predicate; see ShowStepJob for names"}
		}
		return spec, "", nil
	}
	if field := inlineVerifyField(spec); field != "" {
		return spec, "", &ToolError{Code: playbook.CodeBadResult, Message: "verify reference cannot carry an inline predicate", Data: map[string]any{"field": field}}
	}
	curated, ok := m.VerifySpecs[spec.Verify]
	if !ok {
		return spec, "", &ToolError{Code: playbook.CodeVerifyNotFound, Message: "verify predicate not found", Data: map[string]any{"verify": spec.Verify}}
	}
	resolved := curated.Clone()
	allowed := map[string]bool{}
	for _, field := range resolved.AllowOverrides {
		allowed[field] = true
	}
	if len(spec.Tests) != 0 {
		if !allowed["tests"] {
			return spec, "", &ToolError{Code: playbook.CodeVerifyOverride, Message: "verify " + spec.Verify + " does not allow a tests override"}
		}
		resolved.Tests = append([]string(nil), spec.Tests...)
	}
	if len(spec.Options) != 0 {
		if !allowed["options"] {
			return spec, "", &ToolError{Code: playbook.CodeVerifyOverride, Message: "verify " + spec.Verify + " does not allow an options override"}
		}
		options := make(map[string]any, len(spec.Options))
		for key, value := range spec.Options {
			options[key] = value
		}
		resolved.Options = options
	}
	resolved.Verify = ""
	resolved.AllowOverrides = nil
	return resolved, spec.Verify, nil
}

// verifyDecls 把对局快照中的具名谓词整理成按名排序的摘要(name+kind),
// 供 ShowStepJob 给执行席路由用。
func verifyDecls(specs map[string]verify.ResultSpec) []VerifyDecl {
	if len(specs) == 0 {
		return nil
	}
	out := make([]VerifyDecl, 0, len(specs))
	for name, spec := range specs {
		out = append(out, VerifyDecl{Name: name, Kind: spec.Kind})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}
