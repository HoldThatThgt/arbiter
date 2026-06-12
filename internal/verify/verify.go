package verify

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/HoldThatThgt/arbiter/internal/deploy"
	"github.com/HoldThatThgt/arbiter/internal/engineclient"
	"github.com/HoldThatThgt/arbiter/internal/playbook"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// ResultSpec 定义于 playbook 包(共享数据模型);别名保持调用面稳定。
type ResultSpec = playbook.ResultSpec

// refreshDedupe 记录每个 root+match 最近一次"成功" refresh 的回合键:
// 只在 Refresh 成功后写入(失败留给同回合下一个 fact 谓词重试),
// 且每个 root+match 只保留最新回合,天然有界。
var refreshDedupe = struct {
	sync.Mutex
	seen map[string]string
}{seen: map[string]string{}}

type Result struct {
	Spec       ResultSpec `json:"spec"`
	ExitCode   *int       `json:"exit_code,omitempty"`
	IsError    *bool      `json:"is_error,omitempty"`
	Output     string     `json:"output"`
	DurationMS int        `json:"duration_ms"`
	Failure    string     `json:"failure,omitempty"`

	// #33:run/fact 类型化判定。Verdict 是 expect 全子句 AND 的结果;
	// Evidence 按 kind 类型化(RunEvidence/FactEvidence),只为复盘服务;
	// ExpectReport 逐条对照,存于 Task 并经 ReviewTask 透出。
	// 裁决只消费 Verdict 与计数;Evidence 绝不参与判定。
	Verdict      *bool           `json:"verdict,omitempty"`
	Evidence     json.RawMessage `json:"evidence,omitempty"`
	ExpectReport []ClauseReport  `json:"expect_report,omitempty"`
}

type SpecError struct {
	Code    string
	Message string
	Data    any
}

func (e *SpecError) Error() string {
	return e.Message
}

func Execute(ctx context.Context, root string, spec ResultSpec) (Result, error) {
	return ExecuteWithMeta(ctx, root, spec, nil)
}

func ExecuteWithMeta(ctx context.Context, root string, spec ResultSpec, meta map[string]any) (Result, error) {
	spec = normalize(spec)
	if err := Validate(spec); err != nil {
		return Result{}, err
	}
	switch spec.Kind {
	case "shell":
		return runShell(ctx, root, spec), nil
	case "mcp":
		return runTool(ctx, root, spec)
	case "run":
		return runRun(ctx, root, spec, meta)
	case "fact":
		return runFact(ctx, root, spec, meta)
	default:
		return Result{}, &SpecError{Code: playbook.CodeBadResult, Message: "unknown result kind"}
	}
}

func Validate(spec ResultSpec) error {
	// 具名 [Verify] 引用必须先由 match 对照对局快照解析成 curated spec;
	// 引用与内联谓词混搭到达这里即拒绝(深度防御,match 解析侧已先行拦截)。
	if spec.Verify != "" && spec.Kind != "" {
		return &SpecError{Code: playbook.CodeBadResult, Message: "verify reference cannot carry an inline predicate"}
	}
	switch spec.Kind {
	case "run", "fact":
		if err := validateTyped(spec); err != nil {
			return err
		}
	case "shell", "mcp":
		// 键集合封闭:legacy kind 不得携带 run/fact 专属字段。
		if field := typedFieldsForLegacy(spec); field != "" {
			return &SpecError{Code: playbook.CodeBadResult, Message: spec.Kind + " spec must not set " + field}
		}
	default:
		return &SpecError{Code: playbook.CodeBadResult, Message: "unknown result kind"}
	}
	if spec.Kind == "shell" && strings.TrimSpace(spec.Command) == "" {
		return &SpecError{Code: playbook.CodeBadResult, Message: "empty shell command"}
	}
	if spec.Kind == "shell" && len(spec.Expect) != 0 {
		return &SpecError{Code: playbook.CodeBadResult, Message: "shell spec must not set expect"}
	}
	if spec.Kind == "mcp" {
		if strings.TrimSpace(spec.Server) == "" || strings.TrimSpace(spec.Tool) == "" {
			return &SpecError{Code: playbook.CodeBadResult, Message: "incomplete mcp result"}
		}
		if _, err := ParseMCPExpect(spec.Expect); err != nil {
			return err
		}
	}
	if spec.TimeoutS < 0 || spec.TimeoutS > playbook.MaxTimeoutS {
		return &SpecError{Code: playbook.CodeBadResult, Message: "timeout_s out of range"}
	}
	if spec.OutputLines < 0 || spec.OutputLines > playbook.MaxOutputLines {
		return &SpecError{Code: playbook.CodeBadResult, Message: "output_lines out of range"}
	}
	return nil
}

func Pass(result Result) bool {
	if result.Failure != "" {
		return false
	}
	// 类型化判定优先:run/fact 的 verdict 是唯一信号(#33)。
	// shell/mcp 语义保持原样,不受影响。
	if result.Verdict != nil {
		return *result.Verdict
	}
	if result.ExitCode != nil {
		return *result.ExitCode == 0
	}
	if result.IsError != nil {
		return !*result.IsError
	}
	return false
}

func normalize(spec ResultSpec) ResultSpec {
	if spec.TimeoutS == 0 {
		spec.TimeoutS = playbook.DefaultTimeoutS
	}
	if spec.OutputLines == 0 {
		spec.OutputLines = playbook.DefaultOutputLines
	}
	if spec.Arguments == nil {
		spec.Arguments = map[string]any{}
	}
	return spec
}

func runShell(parent context.Context, root string, spec ResultSpec) Result {
	start := time.Now()
	ctx, cancel := context.WithTimeout(parent, time.Duration(spec.TimeoutS)*time.Second)
	defer cancel()

	cmd := exec.Command("/bin/sh", "-c", spec.Command)
	cmd.Dir = root
	cmd.Env = os.Environ()
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	var output capBuffer
	cmd.Stdout = &output
	cmd.Stderr = &output

	result := Result{Spec: spec}
	if err := cmd.Start(); err != nil {
		result.Failure = "spawn_error"
		result.Output = tailLines(output.String(), spec.OutputLines)
		result.DurationMS = int(time.Since(start).Milliseconds())
		return result
	}

	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()

	var waitErr error
	select {
	case waitErr = <-done:
	case <-ctx.Done():
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		waitErr = <-done
		result.Failure = "timeout"
	}

	code := 0
	if waitErr != nil {
		var exitErr *exec.ExitError
		if errors.As(waitErr, &exitErr) {
			code = exitErr.ExitCode()
		} else if result.Failure == "" {
			result.Failure = "spawn_error"
			code = -1
		}
	}
	result.ExitCode = &code
	result.Output = tailLines(output.String(), spec.OutputLines)
	result.DurationMS = int(time.Since(start).Milliseconds())
	return result
}

func runTool(parent context.Context, root string, spec ResultSpec) (Result, error) {
	start := time.Now()
	cfg, err := readServerConfig(root, spec.Server)
	if err != nil {
		return Result{}, err
	}
	ctx, cancel := context.WithTimeout(parent, time.Duration(spec.TimeoutS)*time.Second)
	defer cancel()

	cmd := exec.CommandContext(ctx, cfg.Command, cfg.Args...)
	cmd.Dir = root
	cmd.Env = append(os.Environ(), cfg.envList()...)
	client := mcp.NewClient(&mcp.Implementation{Name: "arbiter-verify", Version: "v1"}, nil)
	session, err := client.Connect(ctx, &mcp.CommandTransport{Command: cmd}, nil)
	result := Result{Spec: spec}
	if err != nil {
		result.Failure = failureForContext(ctx, "transport_error")
		result.Output = ""
		result.DurationMS = int(time.Since(start).Milliseconds())
		return result, nil
	}
	defer session.Close()

	call, err := session.CallTool(ctx, &mcp.CallToolParams{Name: spec.Tool, Arguments: spec.Arguments})
	if err != nil {
		result.Failure = failureForContext(ctx, "transport_error")
		result.Output = err.Error()
		result.DurationMS = int(time.Since(start).Milliseconds())
		return result, nil
	}
	isErr := call.IsError
	result.IsError = &isErr
	if len(spec.Expect) != 0 {
		expect, err := ParseMCPExpect(spec.Expect)
		if err != nil {
			return Result{}, err
		}
		payload, err := mcpPayload(call)
		if err != nil {
			result.Failure = "expect_decode_error"
			result.Output = err.Error()
			result.DurationMS = int(time.Since(start).Milliseconds())
			return result, nil
		}
		verdict, report := CompareMCP(expect, payload)
		result.Verdict = &verdict
		result.ExpectReport = report
	}
	result.Output = tailLines(contentText(call), spec.OutputLines)
	result.DurationMS = int(time.Since(start).Milliseconds())
	return result, nil
}

func runFact(parent context.Context, root string, spec ResultSpec, meta map[string]any) (Result, error) {
	start := time.Now()
	ctx, cancel := context.WithTimeout(parent, time.Duration(spec.TimeoutS)*time.Second)
	defer cancel()

	expect, err := ParseFactExpect(spec.Expect)
	if err != nil {
		return Result{}, err
	}
	engine, err := engineclient.Spawn(ctx, engineclient.RoleQuery, root)
	if err != nil {
		return Result{}, &SpecError{Code: playbook.CodeEngineUnavailable, Message: "fact engine unavailable: " + err.Error()}
	}
	defer engine.Close()

	callMeta := map[string]any{"predicate": "fact"}
	for key, value := range meta {
		callMeta[key] = value
	}
	result := Result{Spec: spec}
	if shouldRefreshFacts(root, meta) {
		if _, err := engine.Refresh(ctx, map[string]any{}, callMeta); err != nil {
			result.Failure = failureForContext(ctx, "engine_error")
			result.Output = err.Error()
			result.DurationMS = int(time.Since(start).Milliseconds())
			return result, nil
		}
		recordFactsRefreshed(root, meta)
	}
	call, err := engine.CallTool(ctx, "search", map[string]any{"query": spec.Query}, callMeta)
	if err != nil {
		result.Failure = failureForContext(ctx, "engine_error")
		result.Output = err.Error()
		result.DurationMS = int(time.Since(start).Milliseconds())
		return result, nil
	}
	isErr := call.IsError
	result.IsError = &isErr
	result.Output = tailLines(engineToolText(call), spec.OutputLines)
	if call.IsError {
		result.DurationMS = int(time.Since(start).Milliseconds())
		return result, nil
	}
	evidence := factEvidenceFromStructured(call.StructuredContent)
	rawEvidence, err := json.Marshal(evidence)
	if err != nil {
		return Result{}, err
	}
	verdict, report := CompareFact(expect, evidence)
	result.Verdict = &verdict
	result.Evidence = rawEvidence
	result.ExpectReport = report
	result.DurationMS = int(time.Since(start).Milliseconds())
	return result, nil
}

// runRun 同步执行 run 谓词:经 EXEC 角色的引擎子进程调用其注册的 `run` 工具
// (engine/arbiter_engine/rpc/__init__.py _DEFAULT_TOOLS),用 ParseRunExpect/CompareRun
// 产出判定与证据。镜像 runFact 的接线方式;spec.TimeoutS 经调用上下文生效。
func runRun(parent context.Context, root string, spec ResultSpec, meta map[string]any) (Result, error) {
	start := time.Now()
	ctx, cancel := context.WithTimeout(parent, time.Duration(spec.TimeoutS)*time.Second)
	defer cancel()

	expect, err := ParseRunExpect(spec.Expect)
	if err != nil {
		return Result{}, err
	}
	engine, err := engineclient.Spawn(ctx, engineclient.RoleExec, root)
	if err != nil {
		return Result{}, &SpecError{Code: playbook.CodeEngineUnavailable, Message: "run engine unavailable: " + err.Error()}
	}
	defer engine.Close()

	callMeta := map[string]any{"predicate": "run"}
	for key, value := range meta {
		callMeta[key] = value
	}
	args := map[string]any{"recipe": spec.Recipe}
	if len(spec.Tests) != 0 {
		args["tests"] = append([]string(nil), spec.Tests...)
	}
	if len(spec.Options) != 0 {
		args["options"] = spec.Options
	}

	result := Result{Spec: spec}
	// 不走 engine.CallTool:run 工具把 overall/passed/failed/per_test/facts
	// 平铺在 tools/call 的 result 顶层,需要拿原始 envelope 自行解码。
	raw, err := engine.Call(ctx, "tools/call", map[string]any{"name": "run", "arguments": args, "_meta": callMeta})
	if err != nil {
		result.Failure = failureForContext(ctx, "engine_error")
		result.Output = err.Error()
		result.DurationMS = int(time.Since(start).Milliseconds())
		return result, nil
	}
	var envelope struct {
		Result json.RawMessage `json:"result"`
	}
	if err := json.Unmarshal(raw, &envelope); err != nil {
		result.Failure = "engine_error"
		result.Output = err.Error()
		result.DurationMS = int(time.Since(start).Milliseconds())
		return result, nil
	}
	var payload struct {
		RunID   string            `json:"run_id"`
		Overall string            `json:"overall"`
		Passed  int               `json:"passed"`
		Failed  int               `json:"failed"`
		PerTest []RunPerTest      `json:"per_test"`
		Facts   *RunFactsEvidence `json:"facts"`
		IsError bool              `json:"isError"`
		Content json.RawMessage   `json:"content"`
	}
	if err := json.Unmarshal(envelope.Result, &payload); err != nil {
		result.Failure = "engine_error"
		result.Output = err.Error()
		result.DurationMS = int(time.Since(start).Milliseconds())
		return result, nil
	}
	isErr := payload.IsError
	result.IsError = &isErr
	result.Output = tailLines(string(payload.Content), spec.OutputLines)
	if payload.IsError {
		result.DurationMS = int(time.Since(start).Milliseconds())
		return result, nil
	}
	evidence := RunEvidence{
		RunID:            payload.RunID,
		Overall:          payload.Overall,
		Passed:           payload.Passed,
		Failed:           payload.Failed,
		FirstFailureName: FirstRunFailure(payload.PerTest),
		TestResults:      RunTestResults(payload.PerTest),
		Facts:            payload.Facts,
	}
	rawEvidence, err := json.Marshal(evidence)
	if err != nil {
		return Result{}, err
	}
	verdict, report := CompareRun(expect, evidence)
	result.Verdict = &verdict
	result.Evidence = rawEvidence
	result.ExpectReport = report
	result.DurationMS = int(time.Since(start).Milliseconds())
	return result, nil
}

type mcpFile struct {
	Servers map[string]serverConfig `json:"mcpServers"`
}

type serverConfig struct {
	Type    string            `json:"type"`
	Command string            `json:"command"`
	Args    []string          `json:"args"`
	Env     map[string]string `json:"env"`
}

func (s serverConfig) envList() []string {
	out := make([]string, 0, len(s.Env))
	for k, v := range s.Env {
		out = append(out, k+"="+v)
	}
	return out
}

func readServerConfig(root, name string) (serverConfig, error) {
	var file mcpFile
	path := deploy.MCPConfigPath(root)
	data, err := os.ReadFile(path)
	if err != nil {
		return serverConfig{}, &SpecError{Code: playbook.CodeServerNotFound, Message: "mcp server not found"}
	}
	if err := json.Unmarshal(data, &file); err != nil {
		return serverConfig{}, &SpecError{Code: playbook.CodeServerNotFound, Message: "mcp server not found"}
	}
	cfg, ok := file.Servers[name]
	if !ok {
		return serverConfig{}, &SpecError{Code: playbook.CodeServerNotFound, Message: "mcp server not found"}
	}
	if cfg.Type != "stdio" {
		return serverConfig{}, &SpecError{Code: playbook.CodeUnsupportedTransport, Message: "only stdio transport is supported"}
	}
	selfPath, err := os.Executable()
	if err != nil {
		return serverConfig{}, &SpecError{Code: playbook.CodeReservedServer, Message: "could not resolve current executable"}
	}
	self, err := resolvedExecutable(selfPath)
	if err != nil {
		return serverConfig{}, &SpecError{Code: playbook.CodeReservedServer, Message: "could not resolve current executable"}
	}
	target, err := resolvedExecutable(cfg.Command)
	if err == nil {
		if target == self || sameFile(target, self) {
			return serverConfig{}, &SpecError{Code: playbook.CodeReservedServer, Message: "reserved server"}
		}
	}
	return cfg, nil
}

func resolvedExecutable(path string) (string, error) {
	var err error
	if !filepath.IsAbs(path) {
		path, err = exec.LookPath(path)
		if err != nil {
			return "", err
		}
	}
	path, err = filepath.Abs(path)
	if err != nil {
		return "", err
	}
	if resolved, err := filepath.EvalSymlinks(path); err == nil {
		path = resolved
	}
	return path, nil
}

func sameFile(left, right string) bool {
	leftInfo, leftErr := os.Stat(left)
	rightInfo, rightErr := os.Stat(right)
	return leftErr == nil && rightErr == nil && os.SameFile(leftInfo, rightInfo)
}

func failureForContext(ctx context.Context, fallback string) string {
	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		return "timeout"
	}
	return fallback
}

func contentText(result *mcp.CallToolResult) string {
	if result == nil {
		return ""
	}
	data, err := json.Marshal(result.Content)
	if err != nil {
		return fmt.Sprint(result.Content)
	}
	return string(data)
}

func mcpPayload(result *mcp.CallToolResult) (any, error) {
	data, err := json.Marshal(result)
	if err != nil {
		return nil, err
	}
	var payload map[string]any
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, err
	}
	payload["isError"] = result.IsError
	return payload, nil
}

func engineToolText(result engineclient.ToolResult) string {
	data, err := json.Marshal(result.Content)
	if err != nil {
		return fmt.Sprint(result.Content)
	}
	return string(data)
}

func factEvidenceFromStructured(payload map[string]any) FactEvidence {
	resultCount := intField(payload, "result_count", 0)
	return FactEvidence{
		SnapshotID:   stringField(payload, "base_snapshot_id"),
		OverlayID:    stringField(payload, "overlay_id"),
		ViewState:    stringField(payload, "view_state"),
		ResultCount:  resultCount,
		Complete:     boolField(payload, "complete", !boolField(payload, "truncated", false)),
		Reachable:    boolField(payload, "reachable", false),
		TotalResults: intField(payload, "total", resultCount),
	}
}

func refreshDedupeKeys(root string, meta map[string]any) (matchKey, roundKey string, ok bool) {
	roundSeq, exists := meta["round_seq"]
	if !exists {
		return "", "", false
	}
	matchID := fmt.Sprint(meta["match_id"])
	return root + "\x00" + matchID, fmt.Sprint(roundSeq), true
}

func shouldRefreshFacts(root string, meta map[string]any) bool {
	matchKey, roundKey, ok := refreshDedupeKeys(root, meta)
	if !ok {
		return true
	}
	refreshDedupe.Lock()
	defer refreshDedupe.Unlock()
	return refreshDedupe.seen[matchKey] != roundKey
}

// recordFactsRefreshed 只在 engine.Refresh 成功后调用:失败不去重,
// 同回合的下一个 fact 谓词会再次尝试 refresh。
func recordFactsRefreshed(root string, meta map[string]any) {
	matchKey, roundKey, ok := refreshDedupeKeys(root, meta)
	if !ok {
		return
	}
	refreshDedupe.Lock()
	defer refreshDedupe.Unlock()
	refreshDedupe.seen[matchKey] = roundKey
}

func stringField(payload map[string]any, name string) string {
	value, _ := payload[name].(string)
	return value
}

func boolField(payload map[string]any, name string, fallback bool) bool {
	value, ok := payload[name].(bool)
	if !ok {
		return fallback
	}
	return value
}

func intField(payload map[string]any, name string, fallback int) int {
	switch value := payload[name].(type) {
	case int:
		return value
	case float64:
		return int(value)
	default:
		return fallback
	}
}

type capBuffer struct {
	buf bytes.Buffer
}

func (b *capBuffer) Write(p []byte) (int, error) {
	remain := playbook.MaxOutputBytes - b.buf.Len()
	if remain > 0 {
		if len(p) > remain {
			_, _ = b.buf.Write(p[:remain])
		} else {
			_, _ = b.buf.Write(p)
		}
	}
	return len(p), nil
}

func (b *capBuffer) String() string {
	return b.buf.String()
}

func tailLines(text string, keep int) string {
	if keep <= 0 {
		return ""
	}
	text = strings.TrimSuffix(text, "\n")
	lines := strings.Split(text, "\n")
	if len(lines) <= keep {
		return text
	}
	return strings.Join(lines[len(lines)-keep:], "\n")
}
