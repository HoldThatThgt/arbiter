package verify

import (
	"bytes"
	"encoding/json"
	"fmt"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
)

// 本文件实现 #33:run/fact 谓词的封闭模式、提交期校验与类型化比较。
// 红线(go-referee.md#ResultSpec):键集合封闭、提交期 fail-closed、
// 裁决只消费枚举与计数 —— evidence 丰富复盘,绝不影响判定。

// ClauseReport 是 expect 逐条对照的复盘记录,存于 Task 并由 ReviewTask 透出。
type ClauseReport struct {
	Path   string `json:"path"`
	Op     string `json:"op"`
	Value  any    `json:"value,omitempty"`
	Actual any    `json:"actual"`
	OK     bool   `json:"ok"`
}

// RunEvidence / FactEvidence 是按 kind 类型化的证据(裁决只读枚举与计数)。
type RunEvidence struct {
	RunID            string            `json:"run_id"`
	Overall          string            `json:"overall"`
	Passed           int               `json:"passed"`
	Failed           int               `json:"failed"`
	FirstFailureName string            `json:"first_failure_name,omitempty"`
	TestResults      map[string]string `json:"test_results,omitempty"`
}

type FactEvidence struct {
	SnapshotID   string `json:"snapshot_id"`
	OverlayID    string `json:"overlay_id,omitempty"`
	ViewState    string `json:"view_state,omitempty"`
	ResultCount  int    `json:"result_count"`
	Complete     bool   `json:"complete"`
	Reachable    bool   `json:"reachable,omitempty"`
	TotalResults int    `json:"total_results,omitempty"`
}

// OverallExpect 接受单枚举值或 {one_of:[...]} 两种封闭写法。
type OverallExpect struct {
	OneOf []string
}

func (o *OverallExpect) UnmarshalJSON(data []byte) error {
	var single string
	if err := json.Unmarshal(data, &single); err == nil {
		if single == "" {
			return fmt.Errorf("overall must not be empty")
		}
		o.OneOf = []string{single}
		return nil
	}
	var wrapped struct {
		OneOf []string `json:"one_of"`
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&wrapped); err != nil {
		return fmt.Errorf("overall must be a string or {one_of:[...]}")
	}
	if len(wrapped.OneOf) == 0 {
		return fmt.Errorf("overall.one_of must not be empty")
	}
	for _, v := range wrapped.OneOf {
		if v == "" {
			return fmt.Errorf("overall.one_of entries must not be empty")
		}
	}
	o.OneOf = wrapped.OneOf
	return nil
}

func (o OverallExpect) matches(actual string) bool {
	for _, v := range o.OneOf {
		if v == actual {
			return true
		}
	}
	return false
}

type TestExpect struct {
	Name   string `json:"name"`
	Result string `json:"result"`
}

type RunExpect struct {
	Overall   *OverallExpect `json:"overall,omitempty"`
	MaxFailed *int           `json:"max_failed,omitempty"`
	MinPassed *int           `json:"min_passed,omitempty"`
	Test      *TestExpect    `json:"test,omitempty"`
}

type FactExpect struct {
	MinResults   *int  `json:"min_results,omitempty"`
	MaxResults   *int  `json:"max_results,omitempty"`
	Complete     *bool `json:"complete,omitempty"`
	Reachable    *bool `json:"reachable,omitempty"`
	TotalAtLeast *int  `json:"total_at_least,omitempty"`
}

func badResult(format string, args ...any) error {
	return &SpecError{Code: playbook.CodeBadResult, Message: fmt.Sprintf(format, args...)}
}

func strictDecode(raw json.RawMessage, target any, what string) error {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(target); err != nil {
		return badResult("%s: %v", what, err)
	}
	return nil
}

// ParseRunExpect 严格解析 run 期望:未知键、空集、不完整 test 子句均 fail-closed。
func ParseRunExpect(raw json.RawMessage) (RunExpect, error) {
	var expect RunExpect
	if len(raw) == 0 {
		return expect, badResult("run expect is required")
	}
	if err := strictDecode(raw, &expect, "run expect"); err != nil {
		return expect, err
	}
	if expect.Overall == nil && expect.MaxFailed == nil && expect.MinPassed == nil && expect.Test == nil {
		return expect, badResult("run expect must contain at least one clause")
	}
	if expect.MaxFailed != nil && *expect.MaxFailed < 0 {
		return expect, badResult("run expect max_failed must be >= 0")
	}
	if expect.MinPassed != nil && *expect.MinPassed < 0 {
		return expect, badResult("run expect min_passed must be >= 0")
	}
	if expect.Test != nil && (expect.Test.Name == "" || expect.Test.Result == "") {
		return expect, badResult("run expect test clause needs name and result")
	}
	return expect, nil
}

// ParseFactExpect 严格解析 fact 期望。
func ParseFactExpect(raw json.RawMessage) (FactExpect, error) {
	var expect FactExpect
	if len(raw) == 0 {
		return expect, badResult("fact expect is required")
	}
	if err := strictDecode(raw, &expect, "fact expect"); err != nil {
		return expect, err
	}
	if expect.MinResults == nil && expect.MaxResults == nil && expect.Complete == nil &&
		expect.Reachable == nil && expect.TotalAtLeast == nil {
		return expect, badResult("fact expect must contain at least one clause")
	}
	for name, v := range map[string]*int{
		"min_results":    expect.MinResults,
		"max_results":    expect.MaxResults,
		"total_at_least": expect.TotalAtLeast,
	} {
		if v != nil && *v < 0 {
			return expect, badResult("fact expect %s must be >= 0", name)
		}
	}
	return expect, nil
}

// DecodeSpec 在提交边界严格解码 ResultSpec:未知顶层键即校验错误。
func DecodeSpec(raw json.RawMessage) (ResultSpec, error) {
	var spec ResultSpec
	if err := strictDecode(raw, &spec, "result spec"); err != nil {
		return spec, err
	}
	return spec, nil
}

// CompareRun 按封闭操作集对照 run 证据,产出整体判定与逐条 expect_report。
// 只读取枚举与计数;verdict = 所有子句 AND。
func CompareRun(expect RunExpect, ev RunEvidence) (bool, []ClauseReport) {
	var report []ClauseReport
	if expect.Overall != nil {
		report = append(report, ClauseReport{
			Path: "overall", Op: "one_of",
			Value: expect.Overall.OneOf, Actual: ev.Overall,
			OK: expect.Overall.matches(ev.Overall),
		})
	}
	if expect.MaxFailed != nil {
		report = append(report, ClauseReport{
			Path: "max_failed", Op: "le",
			Value: *expect.MaxFailed, Actual: ev.Failed,
			OK: ev.Failed <= *expect.MaxFailed,
		})
	}
	if expect.MinPassed != nil {
		report = append(report, ClauseReport{
			Path: "min_passed", Op: "ge",
			Value: *expect.MinPassed, Actual: ev.Passed,
			OK: ev.Passed >= *expect.MinPassed,
		})
	}
	if expect.Test != nil {
		actual, exists := ev.TestResults[expect.Test.Name]
		report = append(report, ClauseReport{
			Path: "test." + expect.Test.Name, Op: "eq",
			Value: expect.Test.Result, Actual: actual,
			OK: exists && actual == expect.Test.Result,
		})
	}
	return allOK(report), report
}

// CompareFact 对照 fact 证据。
func CompareFact(expect FactExpect, ev FactEvidence) (bool, []ClauseReport) {
	var report []ClauseReport
	if expect.MinResults != nil {
		report = append(report, ClauseReport{
			Path: "min_results", Op: "ge",
			Value: *expect.MinResults, Actual: ev.ResultCount,
			OK: ev.ResultCount >= *expect.MinResults,
		})
	}
	if expect.MaxResults != nil {
		report = append(report, ClauseReport{
			Path: "max_results", Op: "le",
			Value: *expect.MaxResults, Actual: ev.ResultCount,
			OK: ev.ResultCount <= *expect.MaxResults,
		})
	}
	if expect.Complete != nil {
		report = append(report, ClauseReport{
			Path: "complete", Op: "eq",
			Value: *expect.Complete, Actual: ev.Complete,
			OK: ev.Complete == *expect.Complete,
		})
	}
	if expect.Reachable != nil {
		report = append(report, ClauseReport{
			Path: "reachable", Op: "eq",
			Value: *expect.Reachable, Actual: ev.Reachable,
			OK: ev.Reachable == *expect.Reachable,
		})
	}
	if expect.TotalAtLeast != nil {
		report = append(report, ClauseReport{
			Path: "total_at_least", Op: "ge",
			Value: *expect.TotalAtLeast, Actual: ev.TotalResults,
			OK: ev.TotalResults >= *expect.TotalAtLeast,
		})
	}
	return allOK(report), report
}

func allOK(report []ClauseReport) bool {
	if len(report) == 0 {
		return false // 无子句不可视为通过:fail-closed
	}
	for _, clause := range report {
		if !clause.OK {
			return false
		}
	}
	return true
}

func validateTyped(spec ResultSpec) error {
	switch spec.Kind {
	case "run":
		if err := rejectForeign(spec, "run", foreignShellMCP, foreignFact); err != nil {
			return err
		}
		if len(spec.Tests) == 0 {
			return badResult("run spec requires tests[]")
		}
		for _, test := range spec.Tests {
			if test == "" {
				return badResult("run tests[] entries must not be empty")
			}
		}
		_, err := ParseRunExpect(spec.Expect)
		return err
	case "fact":
		if err := rejectForeign(spec, "fact", foreignShellMCP, foreignRun); err != nil {
			return err
		}
		if spec.Query == "" {
			return badResult("fact spec requires query")
		}
		_, err := ParseFactExpect(spec.Expect)
		return err
	default:
		return badResult("unknown result kind")
	}
}

type foreignFields func(spec ResultSpec) string

func foreignShellMCP(spec ResultSpec) string {
	switch {
	case spec.Command != "":
		return "command"
	case spec.Server != "":
		return "server"
	case spec.Tool != "":
		return "tool"
	case len(spec.Arguments) != 0:
		return "arguments"
	}
	return ""
}

func foreignRun(spec ResultSpec) string {
	switch {
	case spec.Recipe != "":
		return "recipe"
	case len(spec.Tests) != 0:
		return "tests"
	case len(spec.Options) != 0:
		return "options"
	}
	return ""
}

func foreignFact(spec ResultSpec) string {
	if spec.Query != "" {
		return "query"
	}
	return ""
}

func rejectForeign(spec ResultSpec, kind string, checks ...foreignFields) error {
	for _, check := range checks {
		if field := check(spec); field != "" {
			return badResult("%s spec must not set %s", kind, field)
		}
	}
	return nil
}

// typedFieldsForLegacy 供 shell/mcp 校验拒绝 run/fact 专属字段(键集合封闭)。
// expect 不在此列:mcp kind 自 ADR-0006 起携带 expect[] 子句,由 Validate 按 kind 处理。
func typedFieldsForLegacy(spec ResultSpec) string {
	if field := foreignRun(spec); field != "" {
		return field
	}
	if field := foreignFact(spec); field != "" {
		return field
	}
	return ""
}
