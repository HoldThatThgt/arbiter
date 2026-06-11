package seat

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

const endBook = `---
name: endgame
description: finish on either branch
---

[STEP] only
[StepJob]
finish once
[CheckList]
- verified
[Branch]
success: END
failure: END
`

func TestThreeSeatFlow(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, ".arbiter", "match", "playbook")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "end.md"), []byte(endBook), 0o644); err != nil {
		t.Fatal(err)
	}

	curator := testClient(t, root, Curator)
	player := testClient(t, root, Player)
	executor := testClient(t, root, Executor)

	var read struct {
		Playbooks []any `json:"playbooks"`
	}
	callJSON(t, curator, "ReadPlayBook", map[string]any{}, &read)
	if len(read.Playbooks) != 1 {
		t.Fatalf("read = %#v", read)
	}

	var load map[string]any
	callJSON(t, curator, "LoadPlayBook", map[string]any{"name": "endgame"}, &load)
	taskID := createTask(t, player, "fail once")
	var submit map[string]any
	callJSON(t, executor, "SubmitTask", map[string]any{
		"task_id": taskID,
		"summary": "exit 2 as asked",
		"report":  "failed",
		"result":  map[string]any{"kind": "shell", "command": "exit 2"},
	}, &submit)
	if submit["verdict"] != "fail" {
		t.Fatalf("submit = %#v", submit)
	}
	var noted map[string]any
	callJSON(t, player, "NotePlaybook", map[string]any{"step_id": "only", "note": "exit code is the only verdict"}, &noted)
	if noted["added"] != true {
		t.Fatalf("noted = %#v", noted)
	}
	var show map[string]any
	callJSON(t, player, "ShowStepJob", map[string]any{}, &show)
	step, _ := show["step"].(map[string]any)
	if gotchas, _ := step["gotchas"].([]any); len(gotchas) != 1 {
		t.Fatalf("show step = %#v", step)
	}
	var check map[string]any
	callJSON(t, player, "CheckStepJob", map[string]any{}, &check)
	if check["match"] != "finished_failure" {
		t.Fatalf("check = %#v", check)
	}
	var list struct {
		Tasks []struct {
			TaskID  string `json:"task_id"`
			Status  string `json:"status"`
			Summary string `json:"summary"`
		} `json:"tasks"`
	}
	callJSON(t, player, "ListTask", map[string]any{}, &list)
	if len(list.Tasks) != 1 || list.Tasks[0].TaskID != taskID || list.Tasks[0].Status != "fail" || list.Tasks[0].Summary != "exit 2 as asked" {
		t.Fatalf("list = %#v", list)
	}

	callJSON(t, curator, "LoadPlayBook", map[string]any{"name": "endgame"}, &load)
	callJSON(t, player, "ShowStepJob", map[string]any{}, &show) // 注记已沉淀进棋谱文件,新对局重解析即见
	step, _ = show["step"].(map[string]any)
	if gotchas, _ := step["gotchas"].([]any); len(gotchas) != 1 {
		t.Fatalf("reloaded step = %#v", step)
	}
	taskID = createTask(t, player, "pass once")
	callJSON(t, executor, "SubmitTask", map[string]any{
		"task_id": taskID,
		"summary": "exit 0 as asked",
		"report":  "passed",
		"result":  map[string]any{"kind": "shell", "command": "exit 0"},
	}, &submit)
	callJSON(t, player, "CheckStepJob", map[string]any{}, &check)
	if check["match"] != "finished_success" {
		t.Fatalf("check = %#v", check)
	}
}

func TestLoadPlayBookEmptyNameIncludesAvailable(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, ".arbiter", "match", "playbook")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "end.md"), []byte(endBook), 0o644); err != nil {
		t.Fatal(err)
	}
	curator := testClient(t, root, Curator)
	res, err := curator.CallTool(context.Background(), &mcp.CallToolParams{
		Name:      "LoadPlayBook",
		Arguments: map[string]any{"name": ""},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !res.IsError || len(res.Content) == 0 {
		t.Fatalf("result = %#v", res)
	}
	text, ok := res.Content[0].(*mcp.TextContent)
	if !ok {
		t.Fatalf("content = %#v", res.Content[0])
	}
	var body struct {
		Code string `json:"code"`
		Data struct {
			Available []string `json:"available"`
		} `json:"data"`
	}
	if err := json.Unmarshal([]byte(text.Text), &body); err != nil {
		t.Fatal(err)
	}
	if body.Code != "playbook_not_found" || len(body.Data.Available) != 1 || body.Data.Available[0] != "endgame" {
		t.Fatalf("body = %#v", body)
	}
}

func createTask(t *testing.T, client *mcp.ClientSession, request string) string {
	t.Helper()
	var out struct {
		TaskID string `json:"task_id"`
	}
	callJSON(t, client, "CreateTask", map[string]any{"request": request}, &out)
	if out.TaskID == "" {
		t.Fatalf("empty task id")
	}
	return out.TaskID
}

func testClient(t *testing.T, root, seatName string) *mcp.ClientSession {
	t.Helper()
	server, err := buildServer(root, seatName)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	st, ct := mcp.NewInMemoryTransports()
	ss, err := server.Connect(ctx, st, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = ss.Close() })
	client := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "v1"}, nil)
	cs, err := client.Connect(ctx, ct, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = cs.Close() })
	return cs
}

func callJSON(t *testing.T, client *mcp.ClientSession, name string, args map[string]any, out any) {
	t.Helper()
	res, err := client.CallTool(context.Background(), &mcp.CallToolParams{Name: name, Arguments: args})
	if err != nil {
		t.Fatal(err)
	}
	if res.IsError {
		t.Fatalf("%s error: %#v", name, res.Content)
	}
	if len(res.Content) == 0 {
		t.Fatalf("%s returned no content", name)
	}
	text, ok := res.Content[0].(*mcp.TextContent)
	if !ok {
		t.Fatalf("%s content = %#v", name, res.Content[0])
	}
	if err := json.Unmarshal([]byte(text.Text), out); err != nil {
		t.Fatalf("%s json: %v text=%s", name, err, text.Text)
	}
}
