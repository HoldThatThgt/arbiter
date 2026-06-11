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
	"syscall"
	"time"

	"github.com/HoldThatThgt/arbiter/internal/deploy"
	"github.com/HoldThatThgt/arbiter/internal/playbook"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// ResultSpec 定义于 playbook 包(共享数据模型);别名保持调用面稳定。
type ResultSpec = playbook.ResultSpec

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
	spec = normalize(spec)
	if err := Validate(spec); err != nil {
		return Result{}, err
	}
	switch spec.Kind {
	case "shell":
		return runShell(ctx, root, spec), nil
	case "mcp":
		return runTool(ctx, root, spec)
	case "run", "fact":
		// 评估路径经 seat 的引擎子进程(go-engineclient),由 #37/#43 接线;
		// 在那之前 fail-closed:模式已校验,但绝不在无引擎时給出判定。
		return Result{}, &SpecError{
			Code:    playbook.CodeEngineUnavailable,
			Message: spec.Kind + " predicates evaluate via the seat's engine children; engine wiring lands with #37/#43",
		}
	default:
		return Result{}, &SpecError{Code: playbook.CodeBadResult, Message: "unknown result kind"}
	}
}

func Validate(spec ResultSpec) error {
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
	if spec.Kind == "mcp" {
		if strings.TrimSpace(spec.Server) == "" || strings.TrimSpace(spec.Tool) == "" {
			return &SpecError{Code: playbook.CodeBadResult, Message: "incomplete mcp result"}
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
	result.Output = tailLines(contentText(call), spec.OutputLines)
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
	if err == nil && target == self {
		return serverConfig{}, &SpecError{Code: playbook.CodeReservedServer, Message: "reserved server"}
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
