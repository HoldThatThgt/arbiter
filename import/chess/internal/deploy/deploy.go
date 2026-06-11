package deploy

import (
	"crypto/rand"
	"embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"chess/internal/playbook"
)

const (
	dirPlaybook     = ".chess/playbook"
	dirRun          = ".chess/run"
	dirLog          = ".chess/log"
	fileFormat      = ".chess/FORMAT.md"
	fileSeatKey     = ".chess/run/seat.key"
	fileMCP         = ".mcp.json"
	fileSettings    = ".claude/settings.json"
	fileCurator     = ".claude/agents/chess-curator.md"
	fileSkill       = ".claude/skills/chessplay/SKILL.md"
	fileSkillCreate = ".claude/skills/playbook-create/SKILL.md"
	fileGitignore   = ".gitignore"
	fileExecutor    = ".claude/agents/chess-executor.md"
)

//go:embed templates/*
var templates embed.FS

func Init(root string) (string, error) {
	exe, err := os.Executable()
	if err != nil {
		return "", err
	}
	exe, err = filepath.Abs(exe)
	if err != nil {
		return "", err
	}
	if resolved, err := filepath.EvalSymlinks(exe); err == nil {
		exe = resolved
	}

	for _, dir := range []string{dirPlaybook, dirRun, dirLog, ".claude/agents", ".claude/skills/chessplay", ".claude/skills/playbook-create"} {
		if err := os.MkdirAll(filepath.Join(root, dir), 0o755); err != nil {
			return "", err
		}
	}
	key, err := ensureSeatKey(filepath.Join(root, fileSeatKey))
	if err != nil {
		return "", err
	}
	if err := writeIfMissing(filepath.Join(root, fileFormat), mustTemplate("templates/FORMAT.md"), 0o644); err != nil {
		return "", err
	}
	replacedMCP, err := mergeMCP(filepath.Join(root, fileMCP), exe)
	if err != nil {
		return "", err
	}
	curator := render(mustTemplate("templates/chess-curator.md"), exe, key)
	if err := atomicWrite(filepath.Join(root, fileCurator), []byte(curator), 0o600); err != nil {
		return "", err
	}
	skill := mustTemplate("templates/chessplay.md")
	if err := atomicWrite(filepath.Join(root, fileSkill), []byte(skill), 0o644); err != nil {
		return "", err
	}
	if err := atomicWrite(filepath.Join(root, fileSkillCreate), []byte(mustTemplate("templates/playbook-create.md")), 0o644); err != nil {
		return "", err
	}
	if err := mergeSettings(filepath.Join(root, fileSettings), exe); err != nil {
		return "", err
	}
	if err := appendGitignore(filepath.Join(root, fileGitignore)); err != nil {
		return "", err
	}
	return guidance(exe, key, replacedMCP), nil
}

func MCPConfigPath(root string) string {
	return filepath.Join(root, fileMCP)
}

func ensureSeatKey(path string) (string, error) {
	if data, err := os.ReadFile(path); err == nil {
		key := strings.TrimSpace(string(data))
		if len(key) == playbook.SeatKeyHexLength {
			return key, nil
		}
	}
	var raw [16]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return "", err
	}
	key := hex.EncodeToString(raw[:])
	if err := atomicWrite(path, []byte(key+"\n"), 0o600); err != nil {
		return "", err
	}
	return key, nil
}

func mergeMCP(path, exe string) (bool, error) {
	root, err := readJSON(path)
	if err != nil {
		return false, err
	}
	servers, _ := root["mcpServers"].(map[string]any)
	if servers == nil {
		servers = map[string]any{}
		root["mcpServers"] = servers
	}
	replaced := false
	if existing, ok := servers["chess"].(map[string]any); ok {
		if command, ok := existing["command"].(string); ok && command != "" && command != exe {
			replaced = true
		}
	}
	servers["chess"] = map[string]any{
		"type":    "stdio",
		"command": exe,
		"args":    []any{"serve", "player"},
	}
	return replaced, writeJSON(path, root, 0o644)
}

func mergeSettings(path, exe string) error {
	root, err := readJSON(path)
	if err != nil {
		return err
	}
	perms, _ := root["permissions"].(map[string]any)
	if perms == nil {
		perms = map[string]any{}
		root["permissions"] = perms
	}
	var deny []any
	if existing, ok := perms["deny"].([]any); ok {
		deny = existing
	}
	for _, item := range []string{
		"Read(.chess/playbook/**)",
		"Read(.chess/run/**)",
		"Read(.claude/agents/chess-curator.md)",
		"Read(.claude/agents/chess-executor.md)",
	} {
		if !hasLineValue(deny, item) {
			deny = append(deny, item)
		}
	}
	perms["deny"] = deny
	mergeStopHook(root, exe)
	return writeJSON(path, root, 0o644)
}

// mergeStopHook 注册停止门控 hook(幂等):命令尾词为 "hook stop" 的条目视为
// Chess 所有,刷新其二进制路径;不存在则追加,既有其他 hook 原样保留。
func mergeStopHook(root map[string]any, exe string) {
	hooks, _ := root["hooks"].(map[string]any)
	if hooks == nil {
		hooks = map[string]any{}
		root["hooks"] = hooks
	}
	stops, _ := hooks["Stop"].([]any)
	cmd := exe + " hook stop"
	found := false
	for _, entry := range stops {
		em, ok := entry.(map[string]any)
		if !ok {
			continue
		}
		inner, _ := em["hooks"].([]any)
		for _, h := range inner {
			hm, ok := h.(map[string]any)
			if !ok {
				continue
			}
			c, _ := hm["command"].(string)
			fields := strings.Fields(c)
			if len(fields) >= 3 && fields[len(fields)-2] == "hook" && fields[len(fields)-1] == "stop" {
				hm["command"] = cmd
				found = true
			}
		}
	}
	if !found {
		stops = append(stops, map[string]any{
			"hooks": []any{map[string]any{"type": "command", "command": cmd, "timeout": 10}},
		})
	}
	hooks["Stop"] = stops
}

func appendGitignore(path string) error {
	var lines []string
	if data, err := os.ReadFile(path); err == nil {
		text := strings.TrimSuffix(string(data), "\n")
		if text != "" {
			lines = strings.Split(text, "\n")
		}
	}
	for _, item := range []string{
		".chess/run/",
		".chess/log/",
		".chess/status.json",
		".claude/agents/chess-curator.md",
	} {
		if !hasString(lines, item) {
			lines = append(lines, item)
		}
	}
	return atomicWrite(path, []byte(strings.Join(lines, "\n")+"\n"), 0o644)
}

func readJSON(path string) (map[string]any, error) {
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return map[string]any{}, nil
	}
	if err != nil {
		return nil, err
	}
	if len(strings.TrimSpace(string(data))) == 0 {
		return map[string]any{}, nil
	}
	var out map[string]any
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, err
	}
	if out == nil {
		out = map[string]any{}
	}
	return out, nil
}

func writeJSON(path string, value map[string]any, perm os.FileMode) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	return atomicWrite(path, append(data, '\n'), perm)
}

func writeIfMissing(path, text string, perm os.FileMode) error {
	if _, err := os.Stat(path); err == nil {
		return nil
	}
	return atomicWrite(path, []byte(text), perm)
}

func atomicWrite(path string, data []byte, perm os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".tmp-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	ok := false
	defer func() {
		if !ok {
			_ = os.Remove(tmpName)
		}
	}()
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Chmod(perm); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmpName, path); err != nil {
		return err
	}
	ok = true
	return nil
}

func mustTemplate(name string) string {
	data, err := templates.ReadFile(name)
	if err != nil {
		panic(err)
	}
	return string(data)
}

func render(text, exe, key string) string {
	text = strings.ReplaceAll(text, "{{CHESS_BIN}}", exe)
	return strings.ReplaceAll(text, "{{SEAT_KEY}}", key)
}

func hasLineValue(values []any, target string) bool {
	for _, value := range values {
		if s, ok := value.(string); ok && s == target {
			return true
		}
	}
	return false
}

func hasString(values []string, target string) bool {
	for _, value := range values {
		if value == target {
			return true
		}
	}
	return false
}

func guidance(exe, key string, replacedMCP bool) string {
	msg := fmt.Sprintf(`chess 已部署。剩余两件事:
1. 把棋谱放进 .chess/playbook/(格式见 .chess/FORMAT.md)
2. 提供执行席位 agent: .claude/agents/chess-executor.md,模板如下
   ┌─────────────────────────────────────────────
   │ ---
   │ name: chess-executor
   │ description: 执行 chess 任务并提交可验证结果
   │ tools: Bash, Read, Write, Edit, Glob, Grep,
   │   mcp__chess-executor__SubmitTask, mcp__chess-executor__ReviewTask
   │ mcpServers:
   │   chess-executor:
   │     type: stdio
   │     command: %s
   │     args: [serve, executor]
   │     env:
   │       CHESS_SEAT_KEY: %s
   │ ---
   │ 你是任务执行者。完成提示中的任务后,必须调用 SubmitTask:
   │ task_id 取提示中的编号,summary 一句话概括结果(进全局任务清单,
   │ 供棋手通览与复盘),report 写明做了什么与证据,result 填能
   │ 独立验证完成的谓词——shell 命令(退出码 0 即通过)或 mcp 调用
   │ (server/tool/arguments,应答非错误即通过)。验证耗时长或输出大时,
   │ 可在 result 中附 timeout_s(默认 600)/ output_lines(默认 256)。
   │ 提交后可用 ReviewTask 查看判定;失败可修复后重交。
   └─────────────────────────────────────────────
   (模板已嵌入本机席位凭证;建议把该文件加入 .gitignore,换机后重跑 chess init)
完成后打开 claude 即可使用(试试: /chessplay 修复构建)。
没有现成棋谱?用 /playbook-create 让模型按你的描述起草并注册一份。
已注册 Stop 门控:对局进行中模型无法自行停止(用户中断不受影响)。
`, exe, key)
	if replacedMCP {
		msg += "提示:.mcp.json 中既有 chess 服务器指向不同命令,已覆盖为当前二进制。\n"
	}
	return msg
}
