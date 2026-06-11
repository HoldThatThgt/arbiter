package seat

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/HoldThatThgt/arbiter/internal/playbook"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func TestSeatToolSurface(t *testing.T) {
	cases := []struct {
		seat string
		want []string
	}{
		{Player, []string{"AddPlayBook", "CheckStepJob", "CreateTask", "ListTask", "NotePlaybook", "ReviewTask", "ShowStepJob"}},
		{Curator, []string{"ListTask", "LoadPlayBook", "ReadPlayBook", "ReviewTask"}},
		{Executor, []string{"ListTask", "ReviewTask", "SubmitTask"}},
	}
	for _, tc := range cases {
		t.Run(tc.seat, func(t *testing.T) {
			server, err := buildServer(t.TempDir(), tc.seat)
			if err != nil {
				t.Fatal(err)
			}
			got := listTools(t, server)
			if join(got) != join(tc.want) {
				t.Fatalf("tools = %#v want %#v", got, tc.want)
			}
		})
	}
}

func TestSeatDenied(t *testing.T) {
	root := t.TempDir()
	matchDir := filepath.Join(root, ".arbiter", "match")
	if err := os.MkdirAll(matchDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(matchDir, "seat.key"), []byte("0123456789abcdef0123456789abcdef\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(playbook.SeatEnvKey, "")
	if err := Run(context.Background(), root, Curator); err == nil {
		t.Fatal("expected denied")
	}
	data, err := os.ReadFile(filepath.Join(root, ".arbiter", "match", "log", "journal.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	if !hasEvent(string(data), "seat_denied") {
		t.Fatalf("journal = %s", data)
	}
}

func listTools(t *testing.T, server *mcp.Server) []string {
	t.Helper()
	ctx := context.Background()
	client := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "v1"}, nil)
	st, ct := mcp.NewInMemoryTransports()
	ss, err := server.Connect(ctx, st, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer ss.Close()
	cs, err := client.Connect(ctx, ct, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer cs.Close()
	res, err := cs.ListTools(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, tool := range res.Tools {
		names = append(names, tool.Name)
	}
	sort.Strings(names)
	return names
}

func join(values []string) string {
	sort.Strings(values)
	out := ""
	for _, value := range values {
		out += value + "\n"
	}
	return out
}

func hasEvent(text, target string) bool {
	for _, line := range strings.Split(strings.TrimSpace(text), "\n") {
		var event map[string]any
		if err := json.Unmarshal([]byte(line), &event); err != nil {
			continue
		}
		if event["event"] == target {
			return true
		}
	}
	return false
}
