package seat

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/HoldThatThgt/arbiter/internal/engineclient"
	"github.com/HoldThatThgt/arbiter/internal/journal"
	"github.com/HoldThatThgt/arbiter/internal/match"
	"github.com/HoldThatThgt/arbiter/internal/playbook"
	"github.com/HoldThatThgt/arbiter/internal/verify"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

const (
	Player   = "player"
	Curator  = "curator"
	Executor = "executor"
)

type EmptyInput struct{}

type LoadPlayBookInput struct {
	Name string `json:"name"`
}

type CreateTaskInput struct {
	Request  string   `json:"request"`
	FactRefs []string `json:"fact_refs,omitempty"`
}

type SubmitTaskInput struct {
	TaskID  string            `json:"task_id"`
	Summary string            `json:"summary"`
	Report  string            `json:"report"`
	Result  verify.ResultSpec `json:"result"`
}

type ReviewTaskInput struct {
	TaskID string `json:"task_id"`
}

type NotePlaybookInput struct {
	StepID string `json:"step_id"`
	Note   string `json:"note"`
}

type AddPlayBookInput struct {
	Content string `json:"content"`
}

type callFunc func(context.Context, json.RawMessage) (any, error)

type seatRuntime struct {
	root  string
	query *engineclient.Engine

	mu   sync.Mutex
	exec *engineclient.Engine
}

func (r *seatRuntime) Close() {
	r.mu.Lock()
	exec := r.exec
	r.exec = nil
	r.mu.Unlock()
	if exec != nil {
		_ = exec.Close()
	}
	if r.query != nil {
		_ = r.query.Close()
	}
}

// queryEngine returns the seat's QUERY engine, respawning it first if a
// previous call left it poisoned (cancellation, timeout, protocol fault).
// r.query is set once before the server starts serving, so no runtime lock
// is needed here; Respawn serializes on the engine's own mutex.
func (r *seatRuntime) queryEngine(ctx context.Context) (*engineclient.Engine, error) {
	if r.query == nil {
		return nil, fmt.Errorf("query engine unavailable")
	}
	if err := respawnIfPoisoned(ctx, r.query); err != nil {
		return nil, err
	}
	return r.query, nil
}

func (r *seatRuntime) execEngine(ctx context.Context) (*engineclient.Engine, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.exec != nil {
		if err := respawnIfPoisoned(ctx, r.exec); err != nil {
			return nil, err
		}
		return r.exec, nil
	}
	engine, err := engineclient.Spawn(ctx, engineclient.RoleExec, r.root)
	if err != nil {
		return nil, err
	}
	r.exec = engine
	return engine, nil
}

// respawnIfPoisoned replaces a poisoned child so one failed call does not
// permanently degrade every proxied engine tool for the seat's lifetime.
func respawnIfPoisoned(ctx context.Context, engine *engineclient.Engine) error {
	if !engine.Poisoned() {
		return nil
	}
	return engine.Respawn(ctx)
}

func Run(ctx context.Context, root, seatName string) error {
	if seatName != Player && seatName != Curator && seatName != Executor {
		return fmt.Errorf("unknown seat: %s", seatName)
	}
	if seatName == Curator || seatName == Executor {
		if err := checkKey(root, seatName); err != nil {
			return err
		}
	}
	_ = journal.Append(root, seatName, "seat_started", map[string]any{"pid": os.Getpid()})
	defer journal.Append(root, seatName, "seat_stopped", map[string]any{"pid": os.Getpid()})

	server, runtime, err := buildServerWithRuntime(ctx, root, seatName)
	if err != nil {
		return err
	}
	defer runtime.Close()
	return server.Run(ctx, &mcp.StdioTransport{})
}

func buildServer(root, seatName string) (*mcp.Server, error) {
	server, _, err := buildServerWithRuntime(context.Background(), root, seatName)
	return server, err
}

func buildServerWithRuntime(ctx context.Context, root, seatName string) (*mcp.Server, *seatRuntime, error) {
	server := mcp.NewServer(&mcp.Implementation{Name: "arbiter-" + seatName, Version: "v1"}, nil)
	store := match.New(root, seatName)
	runtime := &seatRuntime{root: root}
	if seatName == Player || seatName == Executor {
		query, err := engineclient.Spawn(ctx, engineclient.RoleQuery, root)
		if err != nil {
			return nil, nil, err
		}
		runtime.query = query
	}
	switch seatName {
	case Player:
		addShowStepJob(server, root, store)
		addCreateTask(server, root, store)
		addCheckStepJob(server, root, store)
		addListTask(server, root, store)
		addReviewTask(server, root, store)
		addNotePlaybook(server, root, store)
		addAddPlayBook(server, root, store)
	case Curator:
		addReadPlayBook(server, root, store)
		addLoadPlayBook(server, root, store)
		addListTask(server, root, store)
		addReviewTask(server, root, store)
	case Executor:
		addSubmitTask(server, root, store)
		addListTask(server, root, store)
		addReviewTask(server, root, store)
	default:
		runtime.Close()
		return nil, nil, fmt.Errorf("unknown seat: %s", seatName)
	}
	if err := addEngineTools(ctx, server, root, seatName, store, runtime); err != nil {
		runtime.Close()
		return nil, nil, err
	}
	return server, runtime, nil
}

func addEngineTools(ctx context.Context, server *mcp.Server, root, seatName string, store *match.Store, runtime *seatRuntime) error {
	if runtime.query == nil {
		return nil
	}
	decls, err := runtime.query.ToolsList(ctx)
	if err != nil {
		return err
	}
	caps, err := store.ActiveCapabilities()
	if err != nil {
		return err
	}
	recipesCap := hasSeatCapability(caps, "recipes")
	for _, decl := range decls {
		gated := isGatedEngineTool(decl.Name)
		if !seatAllowsEngineTool(seatName, decl.Name, recipesCap) {
			continue
		}
		addEngineProxy(server, root, seatName, store, runtime, decl, gated)
	}
	return nil
}

func addEngineProxy(server *mcp.Server, root, seatName string, store *match.Store, runtime *seatRuntime, decl engineclient.ToolDecl, gated bool) {
	server.AddTool(&mcp.Tool{Name: decl.Name, Description: decl.Description, InputSchema: decl.InputSchema}, func(ctx context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		start := time.Now()
		args := map[string]any{}
		if req != nil && req.Params != nil && len(req.Params.Arguments) > 0 {
			if err := json.Unmarshal(req.Params.Arguments, &args); err != nil {
				return errorResult(&match.ToolError{Code: playbook.CodeBadResult, Message: err.Error()}), nil
			}
		}
		var err error
		var result engineclient.ToolResult
		if gated {
			err = store.RequireActiveCapability("recipes")
		}
		if err == nil {
			var engine *engineclient.Engine
			if isExecEngineTool(decl.Name) {
				engine, err = runtime.execEngine(ctx)
			} else {
				engine, err = runtime.queryEngine(ctx)
			}
			if err == nil {
				result, err = engine.CallTool(ctx, decl.Name, args, store.CurrentMeta())
			}
		}
		fields := map[string]any{
			"tool":        decl.Name,
			"args":        args,
			"ok":          err == nil,
			"duration_ms": int(time.Since(start).Milliseconds()),
			"proxy":       true,
		}
		if terr := toolError(err); terr != nil {
			fields["error_code"] = terr.Code
		}
		_ = journal.Append(root, seatName, "tool_called", fields)
		if err != nil {
			if toolError(err) != nil {
				return errorResult(err), nil
			}
			return errorResult(&match.ToolError{Code: playbook.CodeEngineUnavailable, Message: err.Error()}), nil
		}
		return engineResult(result)
	})
}

func seatAllowsEngineTool(seatName, name string, recipesCap bool) bool {
	switch seatName {
	case Player:
		return name == "search" || name == "detail"
	case Executor:
		if name == "search" || name == "detail" || name == "run" || name == "recipe_search" {
			return true
		}
		return recipesCap && isGatedEngineTool(name)
	default:
		return false
	}
}

func isGatedEngineTool(name string) bool {
	return name == "register" || name == "import_recipes" || name == "scan"
}

func hasSeatCapability(values []string, target string) bool {
	for _, value := range values {
		if value == target {
			return true
		}
	}
	return false
}

func isExecEngineTool(name string) bool {
	return name == "run" || name == "recipe_search" || isGatedEngineTool(name)
}

func engineResult(result engineclient.ToolResult) (*mcp.CallToolResult, error) {
	var content []mcp.Content
	for _, item := range result.Content {
		if item["type"] == "text" {
			if text, ok := item["text"].(string); ok {
				content = append(content, &mcp.TextContent{Text: text})
			}
		}
	}
	if len(content) == 0 {
		data, _ := json.Marshal(result.Content)
		content = []mcp.Content{&mcp.TextContent{Text: string(data)}}
	}
	var structured json.RawMessage
	if result.StructuredContent != nil {
		data, err := json.Marshal(result.StructuredContent)
		if err != nil {
			return nil, err
		}
		structured = json.RawMessage(data)
	}
	return &mcp.CallToolResult{
		IsError:           result.IsError,
		Content:           content,
		StructuredContent: structured,
	}, nil
}

func checkKey(root, seatName string) error {
	env := os.Getenv(playbook.SeatEnvKey)
	reason := ""
	if env == "" {
		reason = "missing_env"
	} else {
		data, err := os.ReadFile(filepath.Join(root, ".arbiter", "match", "seat.key"))
		if err != nil {
			reason = "missing_keyfile"
		} else if strings.TrimSpace(string(data)) != env {
			reason = "mismatch"
		}
	}
	if reason == "" {
		return nil
	}
	_ = journal.Append(root, seatName, "seat_denied", map[string]any{"pid": os.Getpid(), "reason": reason})
	return fmt.Errorf("seat denied: %s", reason)
}

func addReadPlayBook(server *mcp.Server, root string, store *match.Store) {
	add(server, root, store.Seat, "ReadPlayBook", "Read all playbooks", emptySchema(), func(ctx context.Context, raw json.RawMessage) (any, error) {
		var in EmptyInput
		if err := decode(raw, &in); err != nil {
			return nil, err
		}
		return store.ReadPlayBook()
	})
}

func addLoadPlayBook(server *mcp.Server, root string, store *match.Store) {
	add(server, root, store.Seat, "LoadPlayBook", "Load a playbook", objectSchema(map[string]any{"name": stringSchema()}, []string{"name"}), func(ctx context.Context, raw json.RawMessage) (any, error) {
		var in LoadPlayBookInput
		if err := decode(raw, &in); err != nil {
			return nil, err
		}
		return store.LoadPlayBook(in.Name)
	})
}

func addShowStepJob(server *mcp.Server, root string, store *match.Store) {
	add(server, root, store.Seat, "ShowStepJob", "Show current step", emptySchema(), func(ctx context.Context, raw json.RawMessage) (any, error) {
		var in EmptyInput
		if err := decode(raw, &in); err != nil {
			return nil, err
		}
		return store.ShowStepJob()
	})
}

func addCreateTask(server *mcp.Server, root string, store *match.Store) {
	props := map[string]any{
		"request":   stringSchema(),
		"fact_refs": map[string]any{"type": "array", "items": stringSchema()},
	}
	add(server, root, store.Seat, "CreateTask", "Create a task", objectSchema(props, []string{"request"}), func(ctx context.Context, raw json.RawMessage) (any, error) {
		var in CreateTaskInput
		if err := decode(raw, &in); err != nil {
			return nil, err
		}
		return store.CreateTaskWithFacts(in.Request, in.FactRefs)
	})
}

func addSubmitTask(server *mcp.Server, root string, store *match.Store) {
	props := map[string]any{
		"task_id": stringSchema(),
		"summary": stringSchema(),
		"report":  stringSchema(),
		"result":  map[string]any{"type": "object"},
	}
	add(server, root, store.Seat, "SubmitTask", "Submit a task with a one-line summary. result is either an inline predicate spec or {\"verify\": \"<name>\"} referencing a named [Verify] predicate from the playbook (ShowStepJob lists the names); a reference may add tests/options only when that predicate declares them in allow_overrides. Playbooks with verify_policy: named accept references only.", objectSchema(props, []string{"task_id", "summary", "report", "result"}), func(ctx context.Context, raw json.RawMessage) (any, error) {
		var in SubmitTaskInput
		if err := decode(raw, &in); err != nil {
			return nil, err
		}
		return store.SubmitTask(ctx, in.TaskID, in.Summary, in.Report, in.Result)
	})
}

func addCheckStepJob(server *mcp.Server, root string, store *match.Store) {
	add(server, root, store.Seat, "CheckStepJob", "Adjudicate current step", emptySchema(), func(ctx context.Context, raw json.RawMessage) (any, error) {
		var in EmptyInput
		if err := decode(raw, &in); err != nil {
			return nil, err
		}
		return store.CheckStepJob(ctx)
	})
}

func addAddPlayBook(server *mcp.Server, root string, store *match.Store) {
	add(server, root, store.Seat, "AddPlayBook", "Register a new playbook", objectSchema(map[string]any{"content": stringSchema()}, []string{"content"}), func(ctx context.Context, raw json.RawMessage) (any, error) {
		var in AddPlayBookInput
		if err := decode(raw, &in); err != nil {
			return nil, err
		}
		if strings.TrimSpace(in.Content) == "" {
			return nil, &match.ToolError{Code: playbook.CodePlaybookInvalid, Message: "empty content"}
		}
		return store.AddPlayBook(in.Content)
	})
}

func addListTask(server *mcp.Server, root string, store *match.Store) {
	add(server, root, store.Seat, "ListTask", "List all tasks with summaries", emptySchema(), func(ctx context.Context, raw json.RawMessage) (any, error) {
		var in EmptyInput
		if err := decode(raw, &in); err != nil {
			return nil, err
		}
		return store.ListTask()
	})
}

func addNotePlaybook(server *mcp.Server, root string, store *match.Store) {
	props := map[string]any{
		"step_id": stringSchema(),
		"note":    stringSchema(),
	}
	add(server, root, store.Seat, "NotePlaybook", "Append a gotcha note to a playbook step", objectSchema(props, []string{"step_id", "note"}), func(ctx context.Context, raw json.RawMessage) (any, error) {
		var in NotePlaybookInput
		if err := decode(raw, &in); err != nil {
			return nil, err
		}
		return store.NotePlaybook(in.StepID, in.Note)
	})
}

func addReviewTask(server *mcp.Server, root string, store *match.Store) {
	add(server, root, store.Seat, "ReviewTask", "Review a task", objectSchema(map[string]any{"task_id": stringSchema()}, []string{"task_id"}), func(ctx context.Context, raw json.RawMessage) (any, error) {
		var in ReviewTaskInput
		if err := decode(raw, &in); err != nil {
			return nil, err
		}
		if strings.TrimSpace(in.TaskID) == "" {
			return nil, &match.ToolError{Code: playbook.CodeTaskNotFound, Message: "task not found"}
		}
		return store.ReviewTask(in.TaskID)
	})
}

func add(server *mcp.Server, root, seatName, name, description string, schema map[string]any, fn callFunc) {
	server.AddTool(&mcp.Tool{Name: name, Description: description, InputSchema: schema}, func(ctx context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		start := time.Now()
		raw := json.RawMessage("{}")
		if req != nil && req.Params != nil && len(req.Params.Arguments) > 0 {
			raw = req.Params.Arguments
		}
		args := rawArgs(raw)
		out, err := fn(ctx, raw)
		fields := map[string]any{
			"tool":        name,
			"args":        args,
			"ok":          err == nil,
			"duration_ms": int(time.Since(start).Milliseconds()),
		}
		if terr := toolError(err); terr != nil {
			fields["error_code"] = terr.Code
		}
		_ = journal.Append(root, seatName, "tool_called", fields)
		if err != nil {
			return errorResult(err), nil
		}
		return successResult(out)
	})
}

func decode(raw json.RawMessage, out any) error {
	if len(raw) == 0 {
		raw = json.RawMessage("{}")
	}
	if err := json.Unmarshal(raw, out); err != nil {
		return &match.ToolError{Code: playbook.CodeBadResult, Message: err.Error()}
	}
	return nil
}

func successResult(out any) (*mcp.CallToolResult, error) {
	data, err := json.Marshal(out)
	if err != nil {
		return nil, err
	}
	return &mcp.CallToolResult{
		Content:           []mcp.Content{&mcp.TextContent{Text: string(data)}},
		StructuredContent: json.RawMessage(data),
	}, nil
}

func errorResult(err error) *mcp.CallToolResult {
	terr := toolError(err)
	if terr == nil {
		terr = &match.ToolError{Code: playbook.CodeStateCorrupt, Message: err.Error()}
	}
	data, _ := json.Marshal(terr)
	return &mcp.CallToolResult{
		IsError: true,
		Content: []mcp.Content{&mcp.TextContent{Text: string(data)}},
	}
}

func toolError(err error) *match.ToolError {
	if err == nil {
		return nil
	}
	if terr, ok := err.(*match.ToolError); ok {
		return terr
	}
	return nil
}

func rawArgs(raw json.RawMessage) any {
	var out any
	if err := json.Unmarshal(raw, &out); err != nil {
		return string(raw)
	}
	return out
}

func emptySchema() map[string]any {
	return map[string]any{"type": "object", "additionalProperties": false}
}

func stringSchema() map[string]any {
	return map[string]any{"type": "string"}
}

func objectSchema(props map[string]any, required []string) map[string]any {
	return map[string]any{
		"type":                 "object",
		"properties":           props,
		"required":             required,
		"additionalProperties": false,
	}
}
