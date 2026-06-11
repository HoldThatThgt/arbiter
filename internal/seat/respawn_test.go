package seat

import (
	"context"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// TestPoisonedQueryEngineRespawnsOnNextProxiedCall poisons the seat's cached
// QUERY engine (a canceled in-flight call kills and poisons the child) and
// asserts the next proxied tool call respawns it instead of returning
// engine_unavailable forever.
func TestPoisonedQueryEngineRespawnsOnNextProxiedCall(t *testing.T) {
	root := repoWithEngine(t)
	server, rt, err := buildServerWithRuntime(context.Background(), root, Player)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(rt.Close)
	client := testClientForServer(t, server)

	search := func() *mcp.CallToolResult {
		t.Helper()
		res, err := client.CallTool(context.Background(), &mcp.CallToolParams{
			Name:      "search",
			Arguments: map[string]any{"query": "callers:main"},
		})
		if err != nil {
			t.Fatal(err)
		}
		return res
	}

	if res := search(); res.IsError {
		t.Fatalf("search before poison = %#v", res.Content)
	}

	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := rt.query.Call(canceled, "tools/list", nil); err == nil {
		t.Fatal("expected canceled call to fail")
	}
	if !rt.query.Poisoned() {
		t.Fatal("query engine not poisoned after canceled call")
	}

	if res := search(); res.IsError {
		t.Fatalf("search after poison = %#v", res.Content)
	}
	if rt.query.Poisoned() {
		t.Fatal("query engine still poisoned after proxied call")
	}
}

// TestPoisonedExecEngineRespawnsOnNextAccess exercises the lazy EXEC engine
// accessor directly: a poisoned cached child must be respawned, not handed
// back dead.
func TestPoisonedExecEngineRespawnsOnNextAccess(t *testing.T) {
	root := repoWithEngine(t)
	rt := &seatRuntime{root: root}
	t.Cleanup(rt.Close)
	ctx := context.Background()

	engine, err := rt.execEngine(ctx)
	if err != nil {
		t.Fatal(err)
	}

	canceled, cancel := context.WithCancel(ctx)
	cancel()
	if _, err := engine.Call(canceled, "tools/list", nil); err == nil {
		t.Fatal("expected canceled call to fail")
	}
	if !engine.Poisoned() {
		t.Fatal("exec engine not poisoned after canceled call")
	}

	again, err := rt.execEngine(ctx)
	if err != nil {
		t.Fatalf("execEngine after poison: %v", err)
	}
	if again.Poisoned() {
		t.Fatal("exec engine still poisoned after accessor respawn")
	}
	if _, err := again.ToolsList(ctx); err != nil {
		t.Fatalf("ToolsList after respawn: %v", err)
	}
}
