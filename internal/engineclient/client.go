package engineclient

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"time"
)

// EngineRole identifies the seat-side engine role passed to the child.
type EngineRole string

const (
	RoleQuery EngineRole = "QUERY"
	RoleExec  EngineRole = "EXEC"
)

// Engine is one line-delimited JSON-RPC stdio child.
type Engine struct {
	cmd    *exec.Cmd
	stdin  io.WriteCloser
	stdout *bufio.Reader

	mu     sync.Mutex
	nextID int64
}

// AsyncRunStatus is the persisted status returned by arbiter/runStatus.
type AsyncRunStatus struct {
	RunID  string          `json:"run_id"`
	Status string          `json:"status"`
	Result json.RawMessage `json:"result,omitempty"`
}

// Spawn starts the Python engine stub for one role in repo.
func Spawn(ctx context.Context, role EngineRole, repo string) (*Engine, error) {
	if role != RoleQuery && role != RoleExec {
		return nil, fmt.Errorf("engine role %q is invalid", role)
	}

	python := os.Getenv("PYTHON")
	if python == "" {
		python = "python3"
	}

	cmd := exec.CommandContext(ctx, python, "-m", "arbiter_engine.rpc")
	cmd.Dir = repo
	cmd.Env = setEnv(os.Environ(),
		"PYTHONPATH", filepath.Join(repo, "engine"),
		"ARBITER_ENGINE_ROLE", string(role),
	)
	cmd.Stderr = os.Stderr

	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	if err := cmd.Start(); err != nil {
		return nil, err
	}

	return &Engine{
		cmd:    cmd,
		stdin:  stdin,
		stdout: bufio.NewReader(stdout),
	}, nil
}

// Call sends one JSON-RPC request and returns the raw response envelope.
func (e *Engine) Call(ctx context.Context, method string, params any) (json.RawMessage, error) {
	e.mu.Lock()
	defer e.mu.Unlock()

	e.nextID++
	request := rpcRequest{
		JSONRPC: "2.0",
		ID:      e.nextID,
		Method:  method,
		Params:  params,
	}

	data, err := json.Marshal(request)
	if err != nil {
		return nil, err
	}
	data = append(data, '\n')
	if _, err := e.stdin.Write(data); err != nil {
		return nil, err
	}

	type readResult struct {
		line []byte
		err  error
	}
	done := make(chan readResult, 1)
	go func() {
		line, err := e.stdout.ReadBytes('\n')
		done <- readResult{line: line, err: err}
	}()

	select {
	case <-ctx.Done():
		if e.cmd.Process != nil {
			_ = e.cmd.Process.Kill()
		}
		return nil, ctx.Err()
	case result := <-done:
		if result.err != nil {
			return nil, result.err
		}
		line := bytes.TrimSpace(result.line)
		if err := validateResponse(line, e.nextID); err != nil {
			return nil, err
		}
		return append(json.RawMessage(nil), line...), nil
	}
}

// StartRun starts a bounded async engine run and returns its persisted run id.
func (e *Engine) StartRun(ctx context.Context, spec any) (string, error) {
	var result AsyncRunStatus
	if err := e.callResult(ctx, "arbiter/startRun", spec, &result); err != nil {
		return "", err
	}
	if result.RunID == "" {
		return "", fmt.Errorf("engine startRun response missing run_id")
	}
	return result.RunID, nil
}

// RunStatus polls a bounded async engine run by persisted run id.
func (e *Engine) RunStatus(ctx context.Context, runID string) (AsyncRunStatus, error) {
	var result AsyncRunStatus
	if err := e.callResult(ctx, "arbiter/runStatus", map[string]string{"run_id": runID}, &result); err != nil {
		return AsyncRunStatus{}, err
	}
	if result.RunID == "" || result.Status == "" {
		return AsyncRunStatus{}, fmt.Errorf("engine runStatus response missing run_id/status")
	}
	return result, nil
}

func (e *Engine) callResult(ctx context.Context, method string, params, target any) error {
	raw, err := e.Call(ctx, method, params)
	if err != nil {
		return err
	}
	var response struct {
		Result json.RawMessage `json:"result"`
	}
	if err := json.Unmarshal(raw, &response); err != nil {
		return err
	}
	if len(response.Result) == 0 {
		return fmt.Errorf("engine response missing result")
	}
	return json.Unmarshal(response.Result, target)
}

// Close sends EOF to the child and waits for it to exit.
func (e *Engine) Close() error {
	_ = e.stdin.Close()

	done := make(chan error, 1)
	go func() {
		done <- e.cmd.Wait()
	}()

	select {
	case err := <-done:
		return err
	case <-time.After(5 * time.Second):
		if e.cmd.Process != nil {
			_ = e.cmd.Process.Kill()
		}
		return <-done
	}
}

type rpcRequest struct {
	JSONRPC string `json:"jsonrpc"`
	ID      int64  `json:"id"`
	Method  string `json:"method"`
	Params  any    `json:"params,omitempty"`
}

type rpcResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      int64           `json:"id"`
	Result  json.RawMessage `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

type rpcError struct {
	Code    int             `json:"code"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data,omitempty"`
}

// EngineError is a JSON-RPC error returned by arbiter-engine with a typed data.kind.
type EngineError struct {
	Code     int
	Message  string
	Kind     string
	Data     json.RawMessage
	Response json.RawMessage
}

func (e *EngineError) Error() string {
	return fmt.Sprintf("engine error %s (%d): %s", e.Kind, e.Code, e.Message)
}

func validateResponse(line []byte, wantID int64) error {
	var response rpcResponse
	if err := json.Unmarshal(line, &response); err != nil {
		return err
	}
	if response.JSONRPC != "2.0" {
		return fmt.Errorf("engine response jsonrpc = %q, want 2.0", response.JSONRPC)
	}
	if response.ID != wantID {
		return fmt.Errorf("engine response id = %d, want %d", response.ID, wantID)
	}
	if response.Error != nil {
		if response.Error.Code == 0 || response.Error.Message == "" {
			return fmt.Errorf("engine response has invalid error shape")
		}
		kind, err := validateErrorKind(response.Error.Data)
		if err != nil {
			return err
		}
		return &EngineError{
			Code:     response.Error.Code,
			Message:  response.Error.Message,
			Kind:     kind,
			Data:     append(json.RawMessage(nil), response.Error.Data...),
			Response: append(json.RawMessage(nil), line...),
		}
	}
	if len(response.Result) == 0 {
		return fmt.Errorf("engine response missing result")
	}
	return nil
}

func validateErrorKind(data json.RawMessage) (string, error) {
	if len(data) == 0 {
		return "", fmt.Errorf("engine response error missing data")
	}
	var payload struct {
		Kind string `json:"kind"`
	}
	if err := json.Unmarshal(data, &payload); err != nil {
		return "", fmt.Errorf("engine response error data is invalid: %w", err)
	}
	if payload.Kind == "" {
		return "", fmt.Errorf("engine response error data missing kind")
	}
	if !knownEngineErrorKind(payload.Kind) {
		return "", fmt.Errorf("unknown engine error kind %q", payload.Kind)
	}
	return payload.Kind, nil
}

func knownEngineErrorKind(kind string) bool {
	switch kind {
	case "briefing_unresolved",
		"capability_revoked",
		"engine_stale",
		"harness_unavailable",
		"invalid_args",
		"invalid_json",
		"invalid_jsonrpc",
		"invalid_meta",
		"invalid_method",
		"invalid_params",
		"invalid_request",
		"line_too_large",
		"lock_timeout",
		"method_not_found",
		"no_snapshot",
		"recipe_pin_mismatch",
		"schema_invalid",
		"tool_not_found":
		return true
	default:
		return false
	}
}

func setEnv(env []string, pairs ...string) []string {
	out := append([]string(nil), env...)
	for i := 0; i < len(pairs); i += 2 {
		key, value := pairs[i], pairs[i+1]
		prefix := key + "="
		replaced := false
		for j, entry := range out {
			if len(entry) >= len(prefix) && entry[:len(prefix)] == prefix {
				out[j] = prefix + value
				replaced = true
				break
			}
		}
		if !replaced {
			out = append(out, prefix+value)
		}
	}
	return out
}
