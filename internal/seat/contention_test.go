package seat

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func TestEightWayExecutorContentionSuite(t *testing.T) {
	result := exerciseEightWayExecutorContention(t, 8)
	if result.Tasks != 8 || result.Passed != 8 || result.Match != "finished_success" {
		t.Fatalf("contention result = %#v", result)
	}
}

type contentionResult struct {
	Tasks  int
	Passed int
	Match  string
}

func exerciseEightWayExecutorContention(t *testing.T, fanout int) contentionResult {
	t.Helper()
	root := repoWithEngine(t)
	writePlaybook(t, root, "end.md", endBook)
	curator := testClient(t, root, Curator)
	player := testClient(t, root, Player)
	var load map[string]any
	callJSON(t, curator, "LoadPlayBook", map[string]any{"name": "endgame"}, &load)

	taskIDs := make([]string, 0, fanout)
	for i := 0; i < fanout; i++ {
		taskIDs = append(taskIDs, createTask(t, player, fmt.Sprintf("task %d", i)))
	}
	executors := make([]*mcp.ClientSession, 0, fanout)
	for i := 0; i < fanout; i++ {
		executors = append(executors, testClient(t, root, Executor))
	}

	var wg sync.WaitGroup
	errs := make(chan error, fanout)
	for i, taskID := range taskIDs {
		client := executors[i]
		taskID := taskID
		wg.Add(1)
		go func() {
			defer wg.Done()
			errs <- submitTask(context.Background(), client, taskID)
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}

	var check map[string]any
	callJSON(t, player, "CheckStepJob", map[string]any{}, &check)
	var list struct {
		Tasks []struct {
			Status string `json:"status"`
		} `json:"tasks"`
	}
	callJSON(t, player, "ListTask", map[string]any{}, &list)
	passed := 0
	for _, task := range list.Tasks {
		if task.Status == "pass" {
			passed++
		}
	}
	matchStatus, _ := check["match"].(string)
	return contentionResult{Tasks: len(list.Tasks), Passed: passed, Match: matchStatus}
}

func submitTask(ctx context.Context, client *mcp.ClientSession, taskID string) error {
	res, err := client.CallTool(ctx, &mcp.CallToolParams{
		Name: "SubmitTask",
		Arguments: map[string]any{
			"task_id": taskID,
			"summary": "done under contention",
			"report":  "passed",
			"result":  map[string]any{"kind": "shell", "command": "exit 0"},
		},
	})
	if err != nil {
		return err
	}
	if res.IsError {
		return fmt.Errorf("SubmitTask returned error: %#v", res.Content)
	}
	if len(res.Content) == 0 {
		return fmt.Errorf("SubmitTask returned no content")
	}
	text, ok := res.Content[0].(*mcp.TextContent)
	if !ok {
		return fmt.Errorf("SubmitTask content = %#v", res.Content[0])
	}
	var out struct {
		Verdict string `json:"verdict"`
	}
	if err := json.Unmarshal([]byte(text.Text), &out); err != nil {
		return err
	}
	if out.Verdict != "pass" {
		return fmt.Errorf("verdict = %q", out.Verdict)
	}
	return nil
}
