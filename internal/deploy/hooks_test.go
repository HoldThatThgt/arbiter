package deploy

import (
	"os"
	"path/filepath"
	"testing"
)

func TestSettingsStopHookMerge(t *testing.T) {
	root := t.TempDir()
	writeJSONFile(t, filepath.Join(root, fileSettings), map[string]any{
		"hooks": map[string]any{
			"Stop": []any{
				map[string]any{"hooks": []any{map[string]any{"type": "command", "command": "other-tool notify"}}},
				map[string]any{"hooks": []any{map[string]any{"type": "command", "command": "/foreign/tool hook stop", "timeout": 10}}},
				map[string]any{"hooks": []any{map[string]any{"type": "command", "command": "/stale/path/arbiter hook stop", "timeout": 10}}},
			},
		},
	})
	if _, err := InitWithOptions(root, testInitOptions()); err != nil {
		t.Fatal(err)
	}
	var settings map[string]any
	readJSONFile(t, filepath.Join(root, fileSettings), &settings)
	stops := settings["hooks"].(map[string]any)["Stop"].([]any)
	if len(stops) != 4 {
		t.Fatalf("stop entries = %d (%#v)", len(stops), stops)
	}
	commands := stopCommands(settings)
	exe := testExecutable(t)
	foreign, collision, stale, claimed := false, false, false, 0
	for _, c := range commands {
		if c == "other-tool notify" {
			foreign = true
		}
		if c == "/foreign/tool hook stop" {
			collision = true
		}
		if c == "/stale/path/arbiter hook stop" {
			stale = true
		}
		if c == exe+" hook stop" {
			claimed++
		}
	}
	if !foreign || !collision || !stale || claimed != 1 {
		t.Fatalf("commands = %#v", commands)
	}

	// 再跑一次不应新增条目
	if _, err := InitWithOptions(root, testInitOptions()); err != nil {
		t.Fatal(err)
	}
	readJSONFile(t, filepath.Join(root, fileSettings), &settings)
	if n := len(settings["hooks"].(map[string]any)["Stop"].([]any)); n != 4 {
		t.Fatalf("stop entries after re-init = %d", n)
	}
}

func stopCommands(settings map[string]any) []string {
	stops, _ := settings["hooks"].(map[string]any)["Stop"].([]any)
	var commands []string
	for _, entry := range stops {
		for _, h := range entry.(map[string]any)["hooks"].([]any) {
			if command, ok := h.(map[string]any)["command"].(string); ok {
				commands = append(commands, command)
			}
		}
	}
	return commands
}

func testExecutable(t *testing.T) string {
	t.Helper()
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	exe, err = filepath.Abs(exe)
	if err != nil {
		t.Fatal(err)
	}
	if resolved, err := filepath.EvalSymlinks(exe); err == nil {
		exe = resolved
	}
	return exe
}
