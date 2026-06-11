package engineclient

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"sync"
	"syscall"
	"time"

	"github.com/HoldThatThgt/arbiter/internal/embeddedengine"
	"github.com/HoldThatThgt/arbiter/internal/journal"
)

// EngineRole identifies the seat-side engine role passed to the child.
type EngineRole string

const (
	RoleQuery EngineRole = "QUERY"
	RoleExec  EngineRole = "EXEC"

	// defaultCallTimeout bounds calls whose parent context carries no
	// deadline. Override per process with callTimeoutEnv.
	defaultCallTimeout = 600 * time.Second
	maxCallTimeout     = 3600 * time.Second
	closeGrace         = 5 * time.Second

	// callTimeoutEnv (ARBITER_ENGINE_CALL_TIMEOUT_S) overrides
	// defaultCallTimeout with a positive integer number of seconds, for
	// recipe stages that legitimately run longer than 600s. Invalid or
	// absent values keep the default. Calls whose parent context already
	// has a deadline are unaffected (maxCallTimeout still caps those).
	callTimeoutEnv = "ARBITER_ENGINE_CALL_TIMEOUT_S"
)

var (
	// ErrPoisoned reports a protocol or timeout failure after which the child must not be reused.
	ErrPoisoned = errors.New("engine child poisoned")
	ErrClosed   = errors.New("engine child closed")
)

// Engine is one line-delimited JSON-RPC stdio child.
type Engine struct {
	cmd    *exec.Cmd
	stdin  io.WriteCloser
	stdout *bufio.Reader
	cfg    spawnConfig

	mu     sync.Mutex
	nextID int64
	poison bool
	closed bool
	waited bool
}

type spawnConfig struct {
	role EngineRole
	repo string
	argv []string
	env  []string
}

// ToolDecl is one tools/list descriptor forwarded by the engine.
type ToolDecl struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"inputSchema"`
}

// ToolResult is a tools/call response forwarded by the engine.
type ToolResult struct {
	Content           []map[string]any `json:"content"`
	StructuredContent map[string]any   `json:"structuredContent,omitempty"`
	IsError           bool             `json:"isError"`
	Namespace         string           `json:"namespace,omitempty"`
	Tool              string           `json:"tool,omitempty"`
}

// RunStart is the arbiter/startRun response.
type RunStart struct {
	RunID string `json:"run_id"`
	State string `json:"state"`
}

// RunStatus is the arbiter/runStatus response.
type RunStatus struct {
	RunID  string          `json:"run_id"`
	State  string          `json:"state"`
	Result json.RawMessage `json:"result,omitempty"`
}

// RefreshResult is the arbiter/refresh response.
type RefreshResult struct {
	Refreshed        bool           `json:"refreshed"`
	Scope            map[string]any `json:"scope"`
	ViewState        string         `json:"view_state"`
	BaseSnapshotID   string         `json:"base_snapshot_id,omitempty"`
	OverlayID        string         `json:"overlay_id,omitempty"`
	StaleSourceCount int            `json:"stale_source_count"`
	PendingTaskCount int            `json:"pending_task_count"`
}

// BriefingCard is one resolved fact card returned by arbiter/resolveBriefing.
type BriefingCard struct {
	Ref     string `json:"ref"`
	Content string `json:"content"`
}

// ResolveBriefingResult is the arbiter/resolveBriefing response.
type ResolveBriefingResult struct {
	Briefing []BriefingCard `json:"briefing"`
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

	pythonPath := filepath.Join(repo, "engine")
	if cfg, ok, err := embeddedConfig(repo); err != nil {
		return nil, err
	} else if ok {
		manifest, verifyErr := embeddedengine.Verify(repo, cfg.Digest)
		fields := map[string]any{"expected": cfg.Digest, "found": manifest.Digest}
		if verifyErr != nil {
			fields["outcome"] = "failed"
			_ = journal.Append(repo, "engine", "embedded_engine_verified", fields)
			return nil, verifyErr
		}
		fields["outcome"] = "ok"
		_ = journal.Append(repo, "engine", "embedded_engine_verified", fields)
		pythonPath = embeddedengine.PythonPath(repo)
	}
	if existing := os.Getenv("PYTHONPATH"); existing != "" {
		pythonPath += string(os.PathListSeparator) + existing
	}
	return spawnConfigured(ctx, spawnConfig{
		role: role,
		repo: repo,
		argv: []string{python, "-m", "arbiter_engine.rpc"},
		env: setEnv(os.Environ(),
			"PYTHONPATH", pythonPath,
			"ARBITER_ENGINE_ROLE", string(role),
		),
	})
}

type embeddedEngineConfig struct {
	Digest string
}

func embeddedConfig(repo string) (embeddedEngineConfig, bool, error) {
	data, err := os.ReadFile(filepath.Join(repo, ".arbiter", "run", "engines.json"))
	if os.IsNotExist(err) {
		return embeddedEngineConfig{}, false, nil
	}
	if err != nil {
		return embeddedEngineConfig{}, false, err
	}
	var raw struct {
		Mode   string `json:"mode"`
		Root   string `json:"engine_root"`
		Digest string `json:"engine_digest"`
	}
	if err := json.Unmarshal(data, &raw); err != nil {
		return embeddedEngineConfig{}, false, err
	}
	if raw.Mode != "embedded" {
		return embeddedEngineConfig{}, false, nil
	}
	if raw.Root != embeddedengine.RootRel || raw.Digest == "" {
		return embeddedEngineConfig{}, false, fmt.Errorf("invalid embedded engine config")
	}
	return embeddedEngineConfig{Digest: raw.Digest}, true, nil
}

func spawnCommand(ctx context.Context, role EngineRole, repo string, argv []string) (*Engine, error) {
	if role != RoleQuery && role != RoleExec {
		return nil, fmt.Errorf("engine role %q is invalid", role)
	}
	if len(argv) == 0 {
		return nil, fmt.Errorf("engine argv is empty")
	}
	return spawnConfigured(ctx, spawnConfig{
		role: role,
		repo: repo,
		argv: append([]string(nil), argv...),
		env:  setEnv(os.Environ(), "ARBITER_ENGINE_ROLE", string(role)),
	})
}

func spawnConfigured(ctx context.Context, cfg spawnConfig) (*Engine, error) {
	// ARBITER_BIN tells engine-side compile stages where the arbiter binary
	// lives so they can build CC='<arbiter> cc -- <real>' interposition
	// without relying on PATH. Left unset when the executable path is
	// unknown.
	if bin, err := os.Executable(); err == nil {
		if abs, absErr := filepath.Abs(bin); absErr == nil {
			bin = abs
		}
		cfg.env = setEnv(cfg.env, "ARBITER_BIN", bin)
	}
	engine := &Engine{cfg: cfg}
	if err := engine.startLocked(ctx); err != nil {
		return nil, err
	}
	return engine, nil
}

func (e *Engine) startLocked(ctx context.Context) error {
	select {
	case <-ctx.Done():
		return ctx.Err()
	default:
	}
	cmd := exec.Command(e.cfg.argv[0], e.cfg.argv[1:]...)
	cmd.Dir = e.cfg.repo
	cmd.Env = append([]string(nil), e.cfg.env...)
	cmd.Stderr = os.Stderr
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	if err := cmd.Start(); err != nil {
		return err
	}

	e.cmd = cmd
	e.stdin = stdin
	e.stdout = bufio.NewReader(stdout)
	e.poison = false
	e.closed = false
	e.waited = false
	return nil
}

// Poisoned reports whether the current child is unsafe to reuse.
func (e *Engine) Poisoned() bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.poison
}

// Respawn replaces a poisoned child with a fresh process using the original spawn config.
func (e *Engine) Respawn(ctx context.Context) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.closed {
		return ErrClosed
	}
	if !e.poison {
		return nil
	}
	return e.startLocked(ctx)
}

// ToolsList forwards tools/list to the live engine.
func (e *Engine) ToolsList(ctx context.Context) ([]ToolDecl, error) {
	data, err := e.Call(ctx, "tools/list", nil)
	if err != nil {
		return nil, err
	}
	var out struct {
		Tools []ToolDecl `json:"tools"`
	}
	if err := decodeResult(data, &out); err != nil {
		return nil, err
	}
	return out.Tools, nil
}

// CallTool forwards tools/call with arguments and optional JSON-RPC _meta.
func (e *Engine) CallTool(ctx context.Context, name string, args, meta any) (ToolResult, error) {
	if args == nil {
		args = map[string]any{}
	}
	params := map[string]any{"name": name, "arguments": args}
	if meta != nil {
		params["_meta"] = meta
	}
	data, err := e.Call(ctx, "tools/call", params)
	if err != nil {
		return ToolResult{}, err
	}
	var result ToolResult
	if err := decodeResult(data, &result); err != nil {
		return ToolResult{}, err
	}
	return result, nil
}

// Refresh asks the QUERY engine to reconcile facts before fact predicates.
func (e *Engine) Refresh(ctx context.Context, scope, meta any) (RefreshResult, error) {
	if scope == nil {
		scope = map[string]any{}
	}
	params := map[string]any{"scope": scope}
	if meta != nil {
		params["_meta"] = meta
	}
	data, err := e.Call(ctx, "arbiter/refresh", params)
	if err != nil {
		return RefreshResult{}, err
	}
	var result RefreshResult
	if err := decodeResult(data, &result); err != nil {
		return RefreshResult{}, err
	}
	return result, nil
}

// ResolveBriefing resolves fact refs into bounded briefing cards.
func (e *Engine) ResolveBriefing(ctx context.Context, refs []string, meta any) (ResolveBriefingResult, error) {
	params := map[string]any{"refs": refs}
	if meta != nil {
		params["_meta"] = meta
	}
	data, err := e.Call(ctx, "arbiter/resolveBriefing", params)
	if err != nil {
		return ResolveBriefingResult{}, err
	}
	var result ResolveBriefingResult
	if err := decodeResult(data, &result); err != nil {
		return ResolveBriefingResult{}, err
	}
	return result, nil
}

// Call sends one JSON-RPC request and returns the raw response envelope.
func (e *Engine) Call(ctx context.Context, method string, params any) (json.RawMessage, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.closed {
		return nil, ErrClosed
	}
	if e.poison {
		return nil, ErrPoisoned
	}

	callCtx, cancel := boundedCallContext(ctx)
	defer cancel()

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
		// A failed or partial stdin write means the child is dead (EPIPE)
		// or the line protocol is desynchronized; it must not be reused.
		e.poisonLocked()
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
	case <-callCtx.Done():
		e.poisonLocked()
		return nil, callCtx.Err()
	case result := <-done:
		if result.err != nil {
			e.poisonLocked()
			return nil, result.err
		}
		line := bytes.TrimSpace(result.line)
		if err := validateResponse(line, e.nextID); err != nil {
			var engineErr *EngineError
			if !errors.As(err, &engineErr) {
				e.poisonLocked()
			}
			return nil, err
		}
		return append(json.RawMessage(nil), line...), nil
	}
}

// StartRun starts a bounded async engine run through the referee-only custom method.
func (e *Engine) StartRun(ctx context.Context, spec, meta any) (RunStart, error) {
	params := map[string]any{"spec": spec}
	if meta != nil {
		params["_meta"] = meta
	}
	data, err := e.Call(ctx, "arbiter/startRun", params)
	if err != nil {
		return RunStart{}, err
	}
	var started RunStart
	if err := decodeResult(data, &started); err != nil {
		return RunStart{}, err
	}
	if started.RunID == "" || started.State == "" {
		return RunStart{}, fmt.Errorf("startRun response missing run_id or state")
	}
	return started, nil
}

// RunStatus polls a bounded async engine run through the referee-only custom method.
func (e *Engine) RunStatus(ctx context.Context, runID string) (RunStatus, error) {
	data, err := e.Call(ctx, "arbiter/runStatus", map[string]any{"run_id": runID})
	if err != nil {
		return RunStatus{}, err
	}
	var status RunStatus
	if err := decodeResult(data, &status); err != nil {
		return RunStatus{}, err
	}
	if status.RunID == "" || status.State == "" {
		return RunStatus{}, fmt.Errorf("runStatus response missing run_id or state")
	}
	return status, nil
}

// Close sends EOF to the child and waits for it to exit.
func (e *Engine) Close() error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.closed {
		return nil
	}
	e.closed = true
	_ = e.stdin.Close()
	return e.waitLocked(closeGrace)
}

func (e *Engine) poisonLocked() {
	if e.poison {
		return
	}
	e.poison = true
	_ = e.stdin.Close()
	_ = killProcessGroup(e.cmd)
	_ = e.waitLocked(2 * time.Second)
}

func (e *Engine) waitLocked(grace time.Duration) error {
	if e.cmd == nil || e.waited {
		return nil
	}
	done := make(chan error, 1)
	cmd := e.cmd
	go func() {
		done <- cmd.Wait()
	}()

	select {
	case err := <-done:
		e.waited = true
		return err
	case <-time.After(grace):
		_ = killProcessGroup(cmd)
		err := <-done
		e.waited = true
		return err
	}
}

func killProcessGroup(cmd *exec.Cmd) error {
	if cmd == nil || cmd.Process == nil {
		return nil
	}
	return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
}

// callTimeout returns the deadline budget for calls without a parent
// deadline: the value of ARBITER_ENGINE_CALL_TIMEOUT_S (positive integer
// seconds) when set and valid, defaultCallTimeout otherwise.
func callTimeout() time.Duration {
	if raw := os.Getenv(callTimeoutEnv); raw != "" {
		if secs, err := strconv.Atoi(raw); err == nil && secs > 0 {
			return time.Duration(secs) * time.Second
		}
	}
	return defaultCallTimeout
}

func boundedCallContext(parent context.Context) (context.Context, context.CancelFunc) {
	now := time.Now()
	limit := now.Add(callTimeout())
	if deadline, ok := parent.Deadline(); ok {
		limit = deadline
		if deadline.Sub(now) > maxCallTimeout {
			limit = now.Add(maxCallTimeout)
		}
	}
	return context.WithDeadline(parent, limit)
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

func decodeResult(line json.RawMessage, target any) error {
	var response rpcResponse
	if err := json.Unmarshal(line, &response); err != nil {
		return err
	}
	if len(response.Result) == 0 {
		return fmt.Errorf("engine response missing result")
	}
	return json.Unmarshal(response.Result, target)
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
