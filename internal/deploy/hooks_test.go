package deploy

import (
	"os"
	"path/filepath"
	"testing"
)

func TestSettingsStopHookMerge(t *testing.T) {
	root := t.TempDir()
	// A live foreign binary literally named "arbiter": it exists on disk, so
	// the merge must never claim or rewrite its hook.
	liveArbiter := filepath.Join(t.TempDir(), "arbiter")
	if err := os.WriteFile(liveArbiter, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	writeJSONFile(t, filepath.Join(root, fileSettings), map[string]any{
		"hooks": map[string]any{
			"Stop": []any{
				map[string]any{"hooks": []any{map[string]any{"type": "command", "command": "other-tool notify"}}},
				map[string]any{"hooks": []any{map[string]any{"type": "command", "command": "/foreign/tool hook stop", "timeout": 10}}},
				map[string]any{"hooks": []any{map[string]any{"type": "command", "command": "/stale/path/arbiter hook stop", "timeout": 10}}},
				map[string]any{"hooks": []any{map[string]any{"type": "command", "command": liveArbiter + " hook stop", "timeout": 10}}},
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
	foreign, collision, stale, live, claimed := false, false, false, false, 0
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
		if c == liveArbiter+" hook stop" {
			live = true
		}
		if c == exe+" hook stop" {
			claimed++
		}
	}
	// 失效的 arbiter 二进制路径应被改写为当前命令,而非新增重复条目;
	// 仍然存在于磁盘上的同名外部二进制不可被劫持。
	if !foreign || !collision || stale || !live || claimed != 1 {
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

	// A live foreign binary that happens to be named "arbiter": a real,
	// executable file on disk must never be claimed.
	liveDir := t.TempDir()
	liveArbiter := filepath.Join(liveDir, "arbiter")
	if err := os.WriteFile(liveArbiter, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	// Pin PATH to an empty dir so bare-name LookPath resolution is
	// deterministic: "arbiter" does not resolve, so it is provably dead.
	t.Setenv("PATH", t.TempDir())

	cases := []struct {
		command string
		want    bool
	}{
		{exe + " hook stop", true},                // exact current command
		{"/stale/path/arbiter hook stop", true},   // dead absolute path → reclaimable
		{"arbiter hook stop", true},               // bare name not on PATH → dead → reclaimable
		{liveArbiter + " hook stop", false},       // live foreign binary named arbiter
		{"/foreign/tool hook stop", false},        // foreign basename
		{"/stale/path/arbiter hook start", false}, // wrong suffix
		{"/stale/path/arbiter notify", false},     // wrong suffix
		{"other-tool notify", false},
		{"", false},
	}
	for _, tc := range cases {
		if got := isArbiterStopHook(tc.command, exe); got != tc.want {
			t.Errorf("isArbiterStopHook(%q) = %t, want %t", tc.command, got, tc.want)
		}
	}

	// A bare "arbiter" that does resolve on PATH is a live binary too.
	t.Setenv("PATH", liveDir)
	if isArbiterStopHook("arbiter hook stop", exe) {
		t.Error("isArbiterStopHook claimed a live PATH-resolvable arbiter")
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
