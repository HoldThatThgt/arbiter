package deploy

import (
	"context"
	"crypto/rand"
	"embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/HoldThatThgt/arbiter/internal/embeddedengine"
	"github.com/HoldThatThgt/arbiter/internal/playbook"
)

const (
	dirPlaybook     = ".arbiter/playbook"
	dirRun          = ".arbiter/run"
	dirMatchRun     = ".arbiter/match/run"
	dirLog          = ".arbiter/match/log"
	fileFormat      = ".arbiter/playbook/FORMAT.md"
	fileSeatKey     = ".arbiter/match/seat.key"
	fileEngines     = ".arbiter/run/engines.json"
	fileConfig      = ".arbiter/config.yml"
	fileRecipes     = ".arbiter/recipes.yaml"
	fileMCP         = ".mcp.json"
	fileSettings    = ".claude/settings.json"
	fileCurator     = ".claude/agents/arbiter-curator.md"
	fileSkill       = ".claude/skills/arbiter-play/SKILL.md"
	fileSkillIntro  = ".claude/skills/arbiter-intro/SKILL.md"
	fileSkillCreate = ".claude/skills/playbook-create/SKILL.md"
	fileGitignore   = ".gitignore"
	fileExecutor    = ".claude/agents/arbiter-executor.md"
)

//go:embed templates/*
var templates embed.FS

type Options struct {
	NoExecutor     bool
	Remove         bool
	EmbeddedEngine bool
	Openings       bool
	Python         string
	FSKind         string
	Now            func() time.Time
	VerifyEngine   func(python, root string) (string, error)
}

type Error struct {
	Kind    string
	Message string
	Err     error
}

func (e *Error) Error() string {
	if e.Message != "" {
		return e.Message
	}
	return e.Kind
}

func (e *Error) Unwrap() error {
	return e.Err
}

func Init(root string) (string, error) {
	return InitWithOptions(root, Options{})
}

func InitWithOptions(root string, opts Options) (string, error) {
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
	if opts.Remove {
		if err := remove(root, exe); err != nil {
			return "", err
		}
		return "arbiter deployment removed.\n", nil
	}

	fsKind := opts.FSKind
	if fsKind == "" {
		fsKind, err = detectFilesystemKind(root)
		if err != nil {
			return "", err
		}
	}
	if isNetworkFilesystem(fsKind) {
		return "", &Error{Kind: "network_filesystem", Message: "arbiter init refused network filesystem: " + fsKind}
	}
	var embeddedDigest string
	if opts.EmbeddedEngine {
		manifest, err := embeddedengine.Unpack(root)
		if err != nil {
			return "", err
		}
		embeddedDigest = manifest.Digest
	}
	python := ResolvePython(opts.Python)
	verify := opts.VerifyEngine
	if verify == nil {
		verify = verifyEngine
	}
	engineVersion, err := verify(python, root)
	if err != nil {
		return "", &Error{Kind: "engine_verify_failed", Message: "arbiter-engine verification failed", Err: err}
	}

	for _, dir := range []string{dirPlaybook, dirRun, dirMatchRun, dirLog, ".arbiter/match", ".claude/agents", ".claude/skills/arbiter-play", ".claude/skills/arbiter-intro", ".claude/skills/playbook-create"} {
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
	if opts.Openings {
		for _, opening := range baseOpenings {
			if err := writeIfMissing(filepath.Join(root, dirPlaybook, opening.file), mustTemplate(opening.template), 0o644); err != nil {
				return "", err
			}
		}
	}
	if err := writeIfMissing(filepath.Join(root, fileConfig), defaultConfig(), 0o644); err != nil {
		return "", err
	}
	if err := writeIfMissing(filepath.Join(root, fileRecipes), defaultRecipes(), 0o644); err != nil {
		return "", err
	}
	if err := writeEngines(filepath.Join(root, fileEngines), python, engineVersion, now(opts), opts.EmbeddedEngine, embeddedDigest); err != nil {
		return "", err
	}
	replacedMCP, err := mergeMCP(filepath.Join(root, fileMCP), exe)
	if err != nil {
		return "", err
	}
	curator := render(mustTemplate("templates/arbiter-curator.md"), exe, key)
	if err := atomicWrite(filepath.Join(root, fileCurator), []byte(curator), 0o600); err != nil {
		return "", err
	}
	if !opts.NoExecutor {
		executor := render(mustTemplate("templates/arbiter-executor.md"), exe, key)
		if err := atomicWrite(filepath.Join(root, fileExecutor), []byte(executor), 0o600); err != nil {
			return "", err
		}
	}
	skill := mustTemplate("templates/arbiter-play.md")
	if err := atomicWrite(filepath.Join(root, fileSkill), []byte(skill), 0o644); err != nil {
		return "", err
	}
	if err := atomicWrite(filepath.Join(root, fileSkillIntro), []byte(mustTemplate("templates/arbiter-intro.md")), 0o644); err != nil {
		return "", err
	}
	if err := atomicWrite(filepath.Join(root, fileSkillCreate), []byte(mustTemplate("templates/playbook-create.md")), 0o644); err != nil {
		return "", err
	}
	if err := mergeSettings(filepath.Join(root, fileSettings), exe, opts.EmbeddedEngine); err != nil {
		return "", err
	}
	if err := appendGitignore(filepath.Join(root, fileGitignore), opts.EmbeddedEngine); err != nil {
		return "", err
	}
	return guidance(replacedMCP, opts.NoExecutor), nil
}

var baseOpenings = []struct {
	file     string
	template string
}{
	{"freeplay.md", "templates/freeplay.md"},
	{"gold-digger.md", "templates/gold-digger.md"},
	{"recipe-derivation.md", "templates/recipe-derivation.md"},
	{"regression-triage.md", "templates/regression-triage.md"},
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
	if existing, ok := servers["arbiter"].(map[string]any); ok {
		if command, ok := existing["command"].(string); ok && command != "" && command != exe {
			replaced = true
		}
	}
	servers["arbiter"] = map[string]any{
		"type":    "stdio",
		"command": exe,
		"args":    []any{"serve", "player"},
	}
	return replaced, writeJSON(path, root, 0o644)
}

func mergeSettings(path, exe string, embedded bool) error {
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
	for _, item := range generatedDenyRules(embedded) {
		if !hasLineValue(deny, item) {
			deny = append(deny, item)
		}
	}
	perms["deny"] = deny
	mergeStopHook(root, exe)
	return writeJSON(path, root, 0o644)
}

// isArbiterStopHook reports whether a Stop hook command is arbiter-owned:
// either the exact current command, or a command whose first field has
// basename "arbiter" (or equals the current exe) and which ends with
// "hook stop". The basename check is the middle ground that lets init and
// remove clean up hooks left behind by moved or rebuilt arbiter binaries
// without hijacking foreign hooks that merely end in "hook stop".
func isArbiterStopHook(command, exe string) bool {
	if command == exe+" hook stop" {
		return true
	}
	fields := strings.Fields(command)
	if len(fields) < 3 || fields[len(fields)-2] != "hook" || fields[len(fields)-1] != "stop" {
		return false
	}
	return fields[0] == exe || filepath.Base(fields[0]) == "arbiter"
}

// mergeStopHook rewrites any arbiter-owned Stop hook to the current command
// (so stale entries from moved/rebuilt binaries do not accumulate) and drops
// duplicates; if no arbiter-owned entry exists it appends a fresh one.
func mergeStopHook(root map[string]any, exe string) {
	hooks, _ := root["hooks"].(map[string]any)
	if hooks == nil {
		hooks = map[string]any{}
		root["hooks"] = hooks
	}
	stops, _ := hooks["Stop"].([]any)
	cmd := exe + " hook stop"
	claimed := false
	var keptStops []any
	for _, entry := range stops {
		em, ok := entry.(map[string]any)
		if !ok {
			keptStops = append(keptStops, entry)
			continue
		}
		inner, _ := em["hooks"].([]any)
		var keptHooks []any
		for _, h := range inner {
			hm, ok := h.(map[string]any)
			if !ok {
				keptHooks = append(keptHooks, h)
				continue
			}
			c, _ := hm["command"].(string)
			if !isArbiterStopHook(c, exe) {
				keptHooks = append(keptHooks, h)
				continue
			}
			if claimed {
				continue // drop duplicate arbiter-owned hooks
			}
			hm["command"] = cmd
			claimed = true
			keptHooks = append(keptHooks, hm)
		}
		if len(inner) > 0 {
			if len(keptHooks) == 0 {
				continue // entry only held dropped arbiter duplicates
			}
			em["hooks"] = keptHooks
		}
		keptStops = append(keptStops, em)
	}
	if !claimed {
		keptStops = append(keptStops, map[string]any{
			"hooks": []any{map[string]any{"type": "command", "command": cmd, "timeout": 10}},
		})
	}
	hooks["Stop"] = keptStops
}

func appendGitignore(path string, embedded bool) error {
	var lines []string
	if data, err := os.ReadFile(path); err == nil {
		text := strings.TrimSuffix(string(data), "\n")
		if text != "" {
			lines = strings.Split(text, "\n")
		}
	}
	for _, item := range generatedGitignoreLines(embedded) {
		if !hasString(lines, item) {
			lines = append(lines, item)
		}
	}
	return atomicWrite(path, []byte(strings.Join(lines, "\n")+"\n"), 0o644)
}

func remove(root, exe string) error {
	if err := removeMCP(filepath.Join(root, fileMCP), exe); err != nil {
		return err
	}
	if err := removeSettings(filepath.Join(root, fileSettings), exe); err != nil {
		return err
	}
	if err := removeGitignore(filepath.Join(root, fileGitignore), isEmbeddedDeployment(root)); err != nil {
		return err
	}
	for _, file := range []string{
		fileEngines, fileSeatKey, fileCurator, fileExecutor, fileSkill, fileSkillIntro,
		fileSkillCreate, fileFormat, fileConfig, fileRecipes,
	} {
		if err := os.Remove(filepath.Join(root, file)); err != nil && !os.IsNotExist(err) {
			return err
		}
	}
	return nil
}

func removeMCP(path, exe string) error {
	root, err := readJSON(path)
	if err != nil {
		return err
	}
	servers, _ := root["mcpServers"].(map[string]any)
	if servers == nil {
		return nil
	}
	if isArbiterServer(servers["arbiter"], exe) {
		delete(servers, "arbiter")
	}
	return writeJSON(path, root, 0o644)
}

func isArbiterServer(value any, exe string) bool {
	server, ok := value.(map[string]any)
	if !ok {
		return false
	}
	args, _ := server["args"].([]any)
	return server["command"] == exe && len(args) == 2 && args[0] == "serve" && args[1] == "player"
}

func removeSettings(path, exe string) error {
	root, err := readJSON(path)
	if err != nil {
		return err
	}
	perms, _ := root["permissions"].(map[string]any)
	if perms != nil {
		if deny, ok := perms["deny"].([]any); ok {
			// Remove the full (embedded) set: every generated rule is
			// arbiter-specific, so stripping rules a non-embedded init never
			// added is harmless and cleans up mode switches.
			perms["deny"] = removeValues(deny, generatedDenyRules(true))
		}
	}
	removeStopHook(root, exe)
	return writeJSON(path, root, 0o644)
}

func removeStopHook(root map[string]any, exe string) {
	hooks, _ := root["hooks"].(map[string]any)
	if hooks == nil {
		return
	}
	stops, _ := hooks["Stop"].([]any)
	var keptStops []any
	for _, entry := range stops {
		em, ok := entry.(map[string]any)
		if !ok {
			keptStops = append(keptStops, entry)
			continue
		}
		inner, _ := em["hooks"].([]any)
		var keptHooks []any
		for _, h := range inner {
			hm, ok := h.(map[string]any)
			if !ok {
				keptHooks = append(keptHooks, h)
				continue
			}
			command, _ := hm["command"].(string)
			if !isArbiterStopHook(command, exe) {
				keptHooks = append(keptHooks, h)
			}
		}
		if len(keptHooks) > 0 {
			em["hooks"] = keptHooks
			keptStops = append(keptStops, em)
		}
	}
	if len(keptStops) == 0 {
		delete(hooks, "Stop")
	} else {
		hooks["Stop"] = keptStops
	}
}

// removeGitignore strips only the lines this deployment mode would have
// added (plus documented legacy lines): a non-embedded init never wrote
// ".arbiter/engine/", so removing it would delete a user's own entry.
func removeGitignore(path string, embedded bool) error {
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	removable := append(generatedGitignoreLines(embedded), legacyGitignoreLines()...)
	lines := strings.Split(strings.TrimSuffix(string(data), "\n"), "\n")
	var kept []string
	for _, line := range lines {
		if line != "" && !hasString(removable, line) {
			kept = append(kept, line)
		}
	}
	return atomicWrite(path, []byte(strings.Join(kept, "\n")+"\n"), 0o644)
}

// isEmbeddedDeployment reports whether init unpacked the embedded engine,
// preferring the engines.json record and falling back to the unpacked engine
// directory when the record is missing or unreadable.
func isEmbeddedDeployment(root string) bool {
	if record, err := readJSON(filepath.Join(root, fileEngines)); err == nil {
		if mode, _ := record["mode"].(string); mode != "" {
			return mode == "embedded"
		}
	}
	info, err := os.Stat(filepath.Join(root, embeddedengine.RootRel))
	return err == nil && info.IsDir()
}

func removeValues(values []any, remove []string) []any {
	var out []any
	for _, value := range values {
		s, ok := value.(string)
		if !ok || !hasString(remove, s) {
			out = append(out, value)
		}
	}
	return out
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

func writeEngines(path, python, version string, at time.Time, embedded bool, digest string) error {
	record := map[string]any{
		"python":         python,
		"engine_version": version,
		"verified_at":    at.UTC().Format(time.RFC3339),
	}
	if embedded {
		record["mode"] = "embedded"
		record["engine_root"] = embeddedengine.RootRel
		record["engine_digest"] = digest
	} else {
		record["mode"] = "installed"
	}
	return writeJSON(path, record, 0o644)
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
	text = strings.ReplaceAll(text, "{{ARBITER_BIN}}", exe)
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

func guidance(replacedMCP, noExecutor bool) string {
	msg := "arbiter 已部署。已写入引擎校验、席位凭证、Claude agents、skills、MCP 与 Stop hook 配置。\n"
	if noExecutor {
		msg += "提示:--no-executor 已跳过 executor agent。\n"
	}
	if replacedMCP {
		msg += "提示:.mcp.json 中既有 arbiter 服务器指向不同命令,已覆盖为当前二进制。\n"
	}
	return msg
}

func defaultConfig() string {
	return "# Arbiter engine config.\nfacts:\n  key_flags: []\n"
}

// defaultRecipes must stay parseable by the engine's strict RecipeBook v2
// parser (engine/arbiter_engine/runs/recipes.py), which requires `targets:`
// to be a sequence and rejects the mapping form `targets: {}`.
func defaultRecipes() string {
	return "# Arbiter RecipeBook v2.\ntargets: []\nprofiles: {}\n"
}

func now(opts Options) time.Time {
	if opts.Now != nil {
		return opts.Now()
	}
	return time.Now().UTC()
}

// ResolvePython resolves the engine python interpreter using the deploy
// resolution order: explicit value, then $ARBITER_ENGINE_PYTHON, then $PYTHON,
// then "python3", with exec.LookPath applied to the winner when possible.
// Other packages (e.g. internal/cli) reuse this so all subprocess call sites
// agree on the interpreter.
func ResolvePython(python string) string {
	if python == "" {
		python = os.Getenv("ARBITER_ENGINE_PYTHON")
	}
	if python == "" {
		python = os.Getenv("PYTHON")
	}
	if python == "" {
		python = "python3"
	}
	if path, err := exec.LookPath(python); err == nil {
		return path
	}
	return python
}

func verifyEngine(python, root string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, python, "-m", "arbiter_engine", "--version")
	cmd.Env = os.Environ()
	if _, err := os.Stat(filepath.Join(root, embeddedengine.RootRel, "arbiter_engine")); err == nil {
		cmd.Env = append(cmd.Env, "PYTHONPATH="+embeddedengine.PythonPath(root))
	} else if _, err := os.Stat(filepath.Join(root, "engine", "arbiter_engine")); err == nil {
		cmd.Env = append(cmd.Env, "PYTHONPATH="+filepath.Join(root, "engine"))
	}
	out, err := cmd.Output()
	if err != nil {
		return "", err
	}
	text := strings.TrimSpace(string(out))
	const prefix = "arbiter-engine "
	if !strings.HasPrefix(text, prefix) {
		return "", fmt.Errorf("unexpected version output %q", text)
	}
	return strings.TrimSpace(strings.TrimPrefix(text, prefix)), nil
}

func detectFilesystemKind(root string) (string, error) {
	if value := os.Getenv("ARBITER_ASSUME_FS"); value != "" {
		return strings.ToLower(value), nil
	}
	if runtime.GOOS == "windows" {
		return "unknown", nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	args := []string{"-f", "%T", root}
	if runtime.GOOS == "linux" {
		args = []string{"-f", "-c", "%T", root}
	}
	out, err := exec.CommandContext(ctx, "stat", args...).Output()
	if err != nil {
		return "unknown", nil
	}
	return strings.ToLower(strings.TrimSpace(string(out))), nil
}

func isNetworkFilesystem(kind string) bool {
	kind = strings.ToLower(kind)
	for _, network := range []string{"nfs", "nfs4", "smbfs", "cifs", "afpfs", "sshfs", "fuse.sshfs"} {
		if kind == network || strings.Contains(kind, network) {
			return true
		}
	}
	return false
}

func generatedDenyRules(embedded bool) []string {
	rules := []string{
		"Read(.arbiter/playbook/**)",
		"Read(.arbiter/match/**)",
		"Read(.claude/agents/arbiter-*.md)",
	}
	if embedded {
		rules = append(rules,
			"Edit(.arbiter/engine/**)",
			"Write(.arbiter/engine/**)",
		)
	}
	return rules
}

func generatedGitignoreLines(embedded bool) []string {
	lines := []string{
		".arbiter/run/",
		".arbiter/match/",
		".arbiter/facts/",
		".arbiter/runs/",
		".arbiter/locks/",
		".claude/agents/arbiter-curator.md",
		".claude/agents/arbiter-executor.md",
	}
	if embedded {
		lines = append(lines, ".arbiter/engine/")
	}
	return lines
}

// legacyGitignoreLines are entries older arbiter versions appended but current
// init no longer writes (".arbiter/match/" already covers status.json).
// removeGitignore still strips them so repos initialized by older binaries do
// not keep dead generated entries behind.
func legacyGitignoreLines() []string {
	return []string{".arbiter/match/status.json"}
}
