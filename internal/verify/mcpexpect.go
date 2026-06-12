package verify

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
)

// 本文件实现 ADR-0006 的 mcp-kind expect[] 子句(go-referee.md#ResultSpec):
// 封闭操作集 eq|ne|ge|le|exists、标量值、≤8 条、点号路径,对照外部(FOREIGN)
// 服务器 structuredContent 的类型化字段。路径缺失与类型不匹配一律 fail-closed
// (包括 ne);文本摘要绝不参与判定。

// MCPClause 是解析后的单条期望;Value 形状随 Op:
// eq/ne → string|bool|float64,ge/le → float64,exists → nil。
type MCPClause struct {
	Path  string
	Op    string
	Value any
}

type mcpClauseWire struct {
	Path  string          `json:"path"`
	Op    string          `json:"op"`
	Value json.RawMessage `json:"value,omitempty"`
}

// ParseMCPExpect 严格解析 mcp expect[]。空输入(未声明 expect)返回 nil;
// 非数组、空数组、超过上限、未知键、空路径、未知操作、非标量值均为校验错误。
func ParseMCPExpect(raw json.RawMessage) ([]MCPClause, error) {
	if len(raw) == 0 {
		return nil, nil
	}
	var wires []mcpClauseWire
	if err := strictDecode(raw, &wires, "mcp expect"); err != nil {
		return nil, err
	}
	if len(wires) == 0 {
		return nil, badResult("mcp expect must contain at least one clause")
	}
	if len(wires) > playbook.MaxExpectClauses {
		return nil, badResult("mcp expect allows at most %d clauses", playbook.MaxExpectClauses)
	}
	clauses := make([]MCPClause, 0, len(wires))
	for i, wire := range wires {
		if wire.Path == "" {
			return nil, badResult("mcp expect clause %d needs path", i)
		}
		clause := MCPClause{Path: wire.Path, Op: wire.Op}
		switch wire.Op {
		case "exists":
			if len(wire.Value) != 0 {
				return nil, badResult("mcp expect clause %d: exists takes no value", i)
			}
		case "eq", "ne":
			value, err := decodeScalar(wire.Value)
			if err != nil {
				return nil, badResult("mcp expect clause %d: %v", i, err)
			}
			clause.Value = value
		case "ge", "le":
			value, err := decodeScalar(wire.Value)
			if err != nil {
				return nil, badResult("mcp expect clause %d: %v", i, err)
			}
			number, ok := value.(float64)
			if !ok {
				return nil, badResult("mcp expect clause %d: %s needs a number value", i, wire.Op)
			}
			clause.Value = number
		default:
			return nil, badResult("mcp expect clause %d: unknown op %q", i, wire.Op)
		}
		clauses = append(clauses, clause)
	}
	return clauses, nil
}

func decodeScalar(raw json.RawMessage) (any, error) {
	if len(raw) == 0 {
		return nil, fmt.Errorf("value is required")
	}
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
		return nil, err
	}
	switch value.(type) {
	case string, bool, float64:
		return value, nil
	}
	return nil, fmt.Errorf("value must be a scalar")
}

// CompareMCP 对照 structuredContent 评估全部子句。isError=true 时判定必为
// 失败(出错的调用不能满足任何期望),子句仍逐条评估以丰富复盘;
// verdict = !isError AND 所有子句成立。
func CompareMCP(clauses []MCPClause, structured any, isError bool) (bool, []ClauseReport) {
	report := make([]ClauseReport, 0, len(clauses))
	for _, clause := range clauses {
		actual, found := resolvePath(structured, clause.Path)
		entry := ClauseReport{Path: clause.Path, Op: clause.Op, Value: clause.Value}
		if found {
			entry.Actual = actual
		}
		switch clause.Op {
		case "exists":
			entry.OK = found
		case "eq":
			entry.OK = found && sameScalarType(actual, clause.Value) && actual == clause.Value
		case "ne":
			entry.OK = found && sameScalarType(actual, clause.Value) && actual != clause.Value
		case "ge":
			number, ok := actual.(float64)
			entry.OK = found && ok && number >= clause.Value.(float64)
		case "le":
			number, ok := actual.(float64)
			entry.OK = found && ok && number <= clause.Value.(float64)
		}
		report = append(report, entry)
	}
	return !isError && allOK(report), report
}

// resolvePath 沿点号路径取值:对象按键、数组按非负十进制下标;任何其他
// 形状(标量中段、越界、非整数下标)都解析失败。
func resolvePath(value any, path string) (any, bool) {
	current := value
	for _, segment := range strings.Split(path, ".") {
		switch node := current.(type) {
		case map[string]any:
			next, ok := node[segment]
			if !ok {
				return nil, false
			}
			current = next
		case []any:
			index, err := strconv.Atoi(segment)
			if err != nil || index < 0 || index >= len(node) {
				return nil, false
			}
			current = node[index]
		default:
			return nil, false
		}
	}
	return current, true
}

func sameScalarType(a, b any) bool {
	switch a.(type) {
	case string:
		_, ok := b.(string)
		return ok
	case bool:
		_, ok := b.(bool)
		return ok
	case float64:
		_, ok := b.(float64)
		return ok
	}
	return false
}
