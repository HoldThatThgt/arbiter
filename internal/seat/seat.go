package seat

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

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

	server, err := buildServer(root, seatName)
	if err != nil {
		return err
	}
	return server.Run(ctx, &mcp.StdioTransport{})
}

func buildServer(root, seatName string) (*mcp.Server, error) {
	server := mcp.NewServer(&mcp.Implementation{Name: "arbiter-" + seatName, Version: "v1"}, nil)
	store := match.New(root, seatName)
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
		return nil, fmt.Errorf("unknown seat: %s", seatName)
	}
	return server, nil
}

func checkKey(root, seatName string) error {
	env := os.Getenv(playbook.SeatEnvKey)
	reason := ""
	if env == "" {
		reason = "missing_env"
	} else {
		data, err := os.ReadFile(filepath.Join(root, ".arbiter", "match", "run", "seat.key"))
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
	add(server, root, store.Seat, "SubmitTask", "Submit a task with a one-line summary", objectSchema(props, []string{"task_id", "summary", "report", "result"}), func(ctx context.Context, raw json.RawMessage) (any, error) {
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
