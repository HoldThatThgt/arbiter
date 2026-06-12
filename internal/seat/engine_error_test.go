package seat

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/HoldThatThgt/arbiter/internal/engineclient"
	"github.com/HoldThatThgt/arbiter/internal/match"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// TestRunUnknownRecipeTeachesThroughProxy drives the executor seat's proxied
// run tool with an unknown recipe id. The engine answers with a structured
// invalid_args error (data.kind/field/detail); the proxy must propagate that
// teaching detail instead of collapsing it into engine_unavailable — that
// code is reserved for spawn/transport failures where the engine never
// answered.
func TestRunUnknownRecipeTeachesThroughProxy(t *testing.T) {
	root := repoWithEngine(t)
	writeCommittedRecipes(t, root)
	server, rt, err := buildServerWithRuntime(context.Background(), root, Executor)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(rt.Close)
	client := testClientForServer(t, server)

	res, err := client.CallTool(context.Background(), &mcp.CallToolParams{
		Name:      "run",
		Arguments: map[string]any{"recipe": "nonexistent"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !res.IsError || len(res.Content) == 0 {
		t.Fatalf("result = %#v", res)
	}
	text := res.Content[0].(*mcp.TextContent).Text
	var body struct {
		Code    string         `json:"code"`
		Message string         `json:"message"`
		Data    map[string]any `json:"data"`
	}
	if err := json.Unmarshal([]byte(text), &body); err != nil {
		t.Fatalf("body %s: %v", text, err)
	}
	if body.Code != "invalid_args" {
		t.Fatalf("code = %q, want invalid_args (engine answered; the args were wrong): %s", body.Code, text)
	}
	if !strings.Contains(body.Message, "unknown recipe 'nonexistent'") {
		t.Fatalf("message %q does not carry the engine's teaching detail", body.Message)
	}
	if body.Data["field"] != "recipe" || body.Data["kind"] != "invalid_args" {
		t.Fatalf("data = %#v, want engine data.kind/field propagated", body.Data)
	}
}

// TestRegisterBadPathTeachesThroughProxy is the register analogue of the run
// test: a GATED engine tool whose engine-side invalid_args (a malformed/missing
// recipe book) must reach the model with field/detail, not collapse into
// engine_unavailable. This is the path the GLM intro stalled on — register
// failed 16x and the model saw only "invalid arguments" with no field.
func TestRegisterBadPathTeachesThroughProxy(t *testing.T) {
	root := repoWithEngine(t)
	writePlaybook(t, root, "recipes.md", recipesBook)
	if _, err := match.New(root, Curator).LoadPlayBook("recipes-flow"); err != nil {
		t.Fatal(err)
	}
	server, rt, err := buildServerWithRuntime(context.Background(), root, Executor)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(rt.Close)
	client := testClientForServer(t, server)

	res, err := client.CallTool(context.Background(), &mcp.CallToolParams{
		Name:      "register",
		Arguments: map[string]any{"path": "nope.yaml"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !res.IsError || len(res.Content) == 0 {
		t.Fatalf("result = %#v", res)
	}
	text := res.Content[0].(*mcp.TextContent).Text
	var body struct {
		Code    string         `json:"code"`
		Message string         `json:"message"`
		Data    map[string]any `json:"data"`
	}
	if err := json.Unmarshal([]byte(text), &body); err != nil {
		t.Fatalf("body %s: %v", text, err)
	}
	if body.Code != "invalid_args" {
		t.Fatalf("code = %q, want invalid_args (engine answered; the path was bad): %s", body.Code, text)
	}
	if body.Data["field"] != "path" || body.Data["detail"] == nil {
		t.Fatalf("data = %#v, want engine field=path + detail propagated", body.Data)
	}
}

// engineToolError keeps the split: structured engine answers become teaching
// ToolErrors with the engine's kind as code; anything else (spawn, transport,
// protocol violations) returns nil so the caller stamps engine_unavailable.
func TestEngineToolErrorMapping(t *testing.T) {
	terr := engineToolError(&engineclient.EngineError{
		Code:    -32602,
		Message: "invalid arguments",
		Kind:    "invalid_args",
		Data:    json.RawMessage(`{"kind":"invalid_args","field":"recipe","detail":"unknown recipe 'x'"}`),
	})
	if terr == nil {
		t.Fatal("structured engine error must map to a ToolError")
	}
	if terr.Code != "invalid_args" {
		t.Fatalf("code = %q", terr.Code)
	}
	if !strings.Contains(terr.Message, "unknown recipe 'x'") || !strings.Contains(terr.Message, `field "recipe"`) {
		t.Fatalf("message = %q", terr.Message)
	}
	data, ok := terr.Data.(map[string]any)
	if !ok || data["detail"] != "unknown recipe 'x'" {
		t.Fatalf("data = %#v", terr.Data)
	}

	wrapped := fmt.Errorf("call failed: %w", &engineclient.EngineError{
		Code:    -32601,
		Message: "tool not found",
		Kind:    "tool_not_found",
		Data:    json.RawMessage(`{"kind":"tool_not_found"}`),
	})
	terr = engineToolError(wrapped)
	if terr == nil || terr.Code != "tool_not_found" || terr.Message != "tool not found" {
		t.Fatalf("wrapped engine error = %#v", terr)
	}

	if terr := engineToolError(errors.New("spawn failed")); terr != nil {
		t.Fatalf("transport failure must stay engine_unavailable, got %#v", terr)
	}
	if terr := engineToolError(nil); terr != nil {
		t.Fatalf("nil error mapped to %#v", terr)
	}
}

// writeCommittedRecipes writes a minimal committed recipe book so the engine's
// run tool gets past book loading and fails on the recipe lookup itself.
func writeCommittedRecipes(t *testing.T, root string) {
	t.Helper()
	book := "targets:\n" +
		"  - id: unit\n" +
		"    harness:\n" +
		"      kind: gtest\n" +
		"    test_run:\n" +
		"      cmd: [/bin/true]\n"
	if err := os.MkdirAll(filepath.Join(root, ".arbiter"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, ".arbiter", "recipes.yaml"), []byte(book), 0o644); err != nil {
		t.Fatal(err)
	}
}
