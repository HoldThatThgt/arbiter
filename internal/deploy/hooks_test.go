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
	if len(stops) != 3 {
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
	// 失效的 arbiter 二进制路径应被改写为当前命令,而非新增重复条目。
	if !foreign || !collision || stale || claimed != 1 {
		t.Fatalf("commands = %#v", commands)
	}

	// 再跑一次不应新增条目
	if _, err := InitWithOptions(root, testInitOptions()); err != nil {
		t.Fatal(err)
	}
	readJSONFile(t, filepath.Join(root, fileSettings), &settings)
	if n := len(settings["hooks"].(map[string]any)["Stop"].([]any)); n != 3 {
		t.Fatalf("stop entries after re-init = %d", n)
	}
}

func TestRemoveStripsStaleArbiterStopHooks(t *testing.T) {
	root := t.TempDir()
	writeJSONFile(t, filepath.Join(root, fileSettings), map[string]any{
		"hooks": map[string]any{
			"Stop": []any{
				map[string]any{"hooks": []any{map[string]any{"type": "command", "command": "/foreign/tool hook stop"}}},
				map[string]any{"hooks": []any{map[string]any{"type": "command", "command": "/moved/elsewhere/arbiter hook stop", "timeout": 10}}},
			},
		},
	})
	opts := testInitOptions()
	opts.Remove = true
	if _, err := InitWithOptions(root, opts); err != nil {
		t.Fatal(err)
	}
	var settings map[string]any
	readJSONFile(t, filepath.Join(root, fileSettings), &settings)
	commands := stopCommands(settings)
	if len(commands) != 1 || commands[0] != "/foreign/tool hook stop" {
		t.Fatalf("commands = %#v", commands)
	}
}

func TestIsArbiterStopHookOwnership(t *testing.T) {
	exe := "/current/arbiter-test"
	cases := []struct {
		command string
		want    bool
	}{
		{exe + " hook stop", true},
		{"/stale/path/arbiter hook stop", true},
		{"arbiter hook stop", true},
		{"/foreign/tool hook stop", false},
		{"/stale/path/arbiter hook start", false},
		{"/stale/path/arbiter notify", false},
		{"other-tool notify", false},
		{"", false},
	}
	for _, tc := range cases {
		if got := isArbiterStopHook(tc.command, exe); got != tc.want {
			t.Errorf("isArbiterStopHook(%q) = %t, want %t", tc.command, got, tc.want)
		}
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
