package seat

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/HoldThatThgt/arbiter/internal/match"
	"github.com/HoldThatThgt/arbiter/internal/playbook"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

const recipesBook = `---
name: recipes-flow
description: grants recipe registration
capabilities: [recipes]
---

[STEP] only
[StepJob]
register recipes
[CheckList]
- done
[Branch]
success: END
failure: END
`

const plainBook = `---
name: plain-flow
description: no recipe capability
---

[STEP] only
[StepJob]
plain
[CheckList]
- done
[Branch]
success: END
failure: END
`

func TestSeatToolSurfaceForwardsEngineTools(t *testing.T) {
	cases := []struct {
		seat string
		want []string
	}{
		{Player, []string{"AddPlayBook", "CheckStepJob", "CreateTask", "ListTask", "NotePlaybook", "ReviewTask", "ShowStepJob", "detail", "search"}},
		{Curator, []string{"ListTask", "LoadPlayBook", "ReadPlayBook", "ReviewTask"}},
		{Executor, []string{"ListTask", "ReviewTask", "SubmitTask", "detail", "recipe_search", "run", "search"}},
	}
	for _, tc := range cases {
		t.Run(tc.seat, func(t *testing.T) {
			root := repoWithEngine(t)
			server, runtime, err := buildServerWithRuntime(context.Background(), root, tc.seat)
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(runtime.Close)
			got := listTools(t, server)
			if join(got) != join(tc.want) {
				t.Fatalf("tools = %#v want %#v", got, tc.want)
			}
		})
	}
}

func TestExecutorGatedToolsRequireRecipesCapability(t *testing.T) {
	root := repoWithEngine(t)
	writePlaybook(t, root, "recipes.md", recipesBook)
	if _, err := match.New(root, Curator).LoadPlayBook("recipes-flow"); err != nil {
		t.Fatal(err)
	}
	server, runtime, err := buildServerWithRuntime(context.Background(), root, Executor)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(runtime.Close)
	got := listTools(t, server)
	for _, name := range []string{"import_recipes", "register", "scan"} {
		if !hasString(got, name) {
			t.Fatalf("missing gated tool %s in %#v", name, got)
		}
	}
}

func TestGatedToolRechecksCapabilityRevoked(t *testing.T) {
	root := repoWithEngine(t)
	writePlaybook(t, root, "recipes.md", recipesBook)
	writePlaybook(t, root, "plain.md", plainBook)
	if _, err := match.New(root, Curator).LoadPlayBook("recipes-flow"); err != nil {
		t.Fatal(err)
	}
	server, runtime, err := buildServerWithRuntime(context.Background(), root, Executor)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(runtime.Close)
	client := testClientForServer(t, server)
	if _, err := match.New(root, Curator).LoadPlayBook("plain-flow"); err != nil {
		t.Fatal(err)
	}
	res, err := client.CallTool(context.Background(), &mcp.CallToolParams{
		Name:      "register",
		Arguments: map[string]any{"path": "missing.yaml"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !res.IsError || len(res.Content) == 0 {
		t.Fatalf("result = %#v", res)
	}
	text := res.Content[0].(*mcp.TextContent).Text
	var body struct {
		Code string `json:"code"`
	}
	if err := json.Unmarshal([]byte(text), &body); err != nil {
		t.Fatal(err)
	}
	if body.Code != playbook.CodeCapabilityRevoked {
		t.Fatalf("body = %#v text=%s", body, text)
	}
}

func repoWithEngine(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("runtime.Caller failed")
	}
	repo := filepath.Clean(filepath.Join(filepath.Dir(file), "..", ".."))
	if err := os.Symlink(filepath.Join(repo, "engine"), filepath.Join(root, "engine")); err != nil {
		t.Fatal(err)
	}
	return root
}

func writePlaybook(t *testing.T, root, name, body string) {
	t.Helper()
	dir := filepath.Join(root, ".arbiter", "playbook")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func testClientForServer(t *testing.T, server *mcp.Server) *mcp.ClientSession {
	t.Helper()
	ctx := context.Background()
	client := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "v1"}, nil)
	st, ct := mcp.NewInMemoryTransports()
	ss, err := server.Connect(ctx, st, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = ss.Close() })
	cs, err := client.Connect(ctx, ct, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = cs.Close() })
	return cs
}

func hasString(values []string, target string) bool {
	for _, value := range values {
		if strings.EqualFold(value, target) {
			return true
		}
	}
	return false
}
