package deploy

import (
	"path/filepath"
	"strings"
	"testing"
)

func TestSettingsStopHookMerge(t *testing.T) {
	root := t.TempDir()
	writeJSONFile(t, filepath.Join(root, fileSettings), map[string]any{
		"hooks": map[string]any{
			"Stop": []any{
				map[string]any{"hooks": []any{map[string]any{"type": "command", "command": "other-tool notify"}}},
				map[string]any{"hooks": []any{map[string]any{"type": "command", "command": "/stale/path/arbiter hook stop", "timeout": 10}}},
			},
		},
	})
	if _, err := Init(root); err != nil {
		t.Fatal(err)
	}
	var settings map[string]any
	readJSONFile(t, filepath.Join(root, fileSettings), &settings)
	stops := settings["hooks"].(map[string]any)["Stop"].([]any)
	if len(stops) != 2 {
		t.Fatalf("stop entries = %d (%#v)", len(stops), stops)
	}
	var commands []string
	for _, entry := range stops {
		for _, h := range entry.(map[string]any)["hooks"].([]any) {
			commands = append(commands, h.(map[string]any)["command"].(string))
		}
	}
	foreign, claimed := false, false
	for _, c := range commands {
		if c == "other-tool notify" {
			foreign = true
		}
		fields := strings.Fields(c)
		if len(fields) >= 3 && fields[len(fields)-2] == "hook" && fields[len(fields)-1] == "stop" {
			if fields[0] == "/stale/path/arbiter" {
				t.Fatalf("stale path not reclaimed: %q", c)
			}
			claimed = true
		}
	}
	if !foreign || !claimed {
		t.Fatalf("commands = %#v", commands)
	}

	// 再跑一次不应新增条目
	if _, err := Init(root); err != nil {
		t.Fatal(err)
	}
	readJSONFile(t, filepath.Join(root, fileSettings), &settings)
	if n := len(settings["hooks"].(map[string]any)["Stop"].([]any)); n != 2 {
		t.Fatalf("stop entries after re-init = %d", n)
	}
}
