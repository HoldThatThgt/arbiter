package seat

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/HoldThatThgt/arbiter/internal/match"
	"github.com/HoldThatThgt/arbiter/internal/playbook"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

const strictDecodeBook = `---
name: strictdecode
description: strict decode flow
---

[Verify] pass
shell: exit 0

[STEP] only
[StepJob]
do it
[CheckList]
- done
[Branch]
success: only
failure: only
`

// callToolErrorBody 调用工具并断言其返回 IsError,解出席位的 typed error。
func callToolErrorBody(t *testing.T, client *mcp.ClientSession, name string, args map[string]any) (code, message string) {
	t.Helper()
	res, err := client.CallTool(context.Background(), &mcp.CallToolParams{Name: name, Arguments: args})
	if err != nil {
		t.Fatal(err)
	}
	if !res.IsError || len(res.Content) == 0 {
		t.Fatalf("%s expected typed error, got %#v", name, res.Content)
	}
	text, ok := res.Content[0].(*mcp.TextContent)
	if !ok {
		t.Fatalf("%s content = %#v", name, res.Content[0])
	}
	var body struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	}
	if err := json.Unmarshal([]byte(text.Text), &body); err != nil {
		t.Fatalf("%s json: %v text=%s", name, err, text.Text)
	}
	return body.Code, body.Message
}

func TestSubmitTaskRejectsUnknownTopLevelKey(t *testing.T) {
	root := repoWithEngine(t)
	writePlaybook(t, root, "strict.md", strictDecodeBook)
	if _, err := match.New(root, Curator).LoadPlayBook("strictdecode"); err != nil {
		t.Fatal(err)
	}
	player := testClient(t, root, Player)
	executor := testClient(t, root, Executor)
	taskID := createTask(t, player, "do it")

	code, message := callToolErrorBody(t, executor, "SubmitTask", map[string]any{
		"task_id":   taskID,
		"summary":   "smuggle a top-level key",
		"report":    "should be rejected",
		"result":    map[string]any{"kind": "shell", "command": "true"},
		"bogus_top": 1,
	})
	if code != playbook.CodeBadResult {
		t.Fatalf("code = %q want %q", code, playbook.CodeBadResult)
	}
	if !strings.Contains(message, "bogus_top") {
		t.Fatalf("message = %q must name the unknown field", message)
	}

	// 被拒提交不得触碰对局状态:任务仍 open。
	var list struct {
		Tasks []struct {
			TaskID string `json:"task_id"`
			Status string `json:"status"`
		} `json:"tasks"`
	}
	callJSON(t, executor, "ListTask", map[string]any{}, &list)
	if len(list.Tasks) != 1 || list.Tasks[0].Status != match.TaskOpen {
		t.Fatalf("list = %#v", list)
	}
}

func TestSubmitTaskRejectsUnknownKeyInsideResultSpec(t *testing.T) {
	root := repoWithEngine(t)
	writePlaybook(t, root, "strict.md", strictDecodeBook)
	if _, err := match.New(root, Curator).LoadPlayBook("strictdecode"); err != nil {
		t.Fatal(err)
	}
	player := testClient(t, root, Player)
	executor := testClient(t, root, Executor)
	taskID := createTask(t, player, "do it")

	code, message := callToolErrorBody(t, executor, "SubmitTask", map[string]any{
		"task_id": taskID,
		"summary": "smuggle a nested key",
		"report":  "should be rejected",
		"result":  map[string]any{"kind": "shell", "command": "true", "bogus": 1},
	})
	if code != playbook.CodeBadResult {
		t.Fatalf("code = %q want %q", code, playbook.CodeBadResult)
	}
	if !strings.Contains(message, "bogus") {
		t.Fatalf("message = %q must name the unknown field", message)
	}
}

func TestSubmitTaskLegalPayloadsStillDecode(t *testing.T) {
	root := repoWithEngine(t)
	writePlaybook(t, root, "strict.md", strictDecodeBook)
	if _, err := match.New(root, Curator).LoadPlayBook("strictdecode"); err != nil {
		t.Fatal(err)
	}
	player := testClient(t, root, Player)
	executor := testClient(t, root, Executor)

	// 内联 shell 谓词带 timeout_s / output_lines:全部合法键必须照常解码。
	inlineTask := createTask(t, player, "inline shell")
	var submit map[string]any
	callJSON(t, executor, "SubmitTask", map[string]any{
		"task_id": inlineTask,
		"summary": "inline shell with options",
		"report":  "passed",
		"result": map[string]any{
			"kind":         "shell",
			"command":      "exit 0",
			"timeout_s":    5,
			"output_lines": 10,
		},
	}, &submit)
	if submit["verdict"] != match.TaskPass {
		t.Fatalf("inline submit = %#v", submit)
	}

	// 具名 [Verify] 引用 {"verify": "..."} 同样照常解码并落入 store。
	namedTask := createTask(t, player, "named verify")
	callJSON(t, executor, "SubmitTask", map[string]any{
		"task_id": namedTask,
		"summary": "named verify reference",
		"report":  "passed",
		"result":  map[string]any{"verify": "pass"},
	}, &submit)
	if submit["verdict"] != match.TaskPass {
		t.Fatalf("named submit = %#v", submit)
	}

	var list struct {
		Tasks []struct {
			TaskID string `json:"task_id"`
			Status string `json:"status"`
		} `json:"tasks"`
	}
	callJSON(t, executor, "ListTask", map[string]any{}, &list)
	if len(list.Tasks) != 2 {
		t.Fatalf("list = %#v", list)
	}
	for _, task := range list.Tasks {
		if task.Status != match.TaskPass {
			t.Fatalf("task %s status = %q want %q", task.TaskID, task.Status, match.TaskPass)
		}
	}
}
