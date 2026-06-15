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
	fileTestAuthor  = ".claude/agents/arbiter-test-author.md"
	fileImplementer = ".claude/agents/arbiter-implementer.md"
	fileDebugger    = ".claude/agents/arbiter-debugger.md"
)

// companion 是随 arbiter 一体交付的伙伴诊断 MCP 服务器(ADR-0010):
// FOREIGN stdio 服务器,永不充当席位。经引擎解释器(python -m)拉起,
// 而非本二进制 —— mcp-kind 谓词的 deny-self 守卫(ADR-0006)因此不受影响。
type companion struct {
	Name       string
	Module     string
	Serve      []string
	Tools      []string
	Command    string
	Args       []string
	PythonPath string
}

var companionSpecs = []companion{
	{
		Name:   "gdb-mcp",
		Module: "arbiter_engine.gdbmcp",
		Serve:  []string{"serve"},
		Tools: []string{
			"gdb_start", "gdb_exec", "gdb_breakpoint", "gdb_select", "gdb_stack",
			"gdb_snapshot", "gdb_eval", "gdb_memory", "gdb_command", "gdb_sessions",
			"gdb_stop", "gdb_diagnostics",
		},
	},
	{
		Name:   "perf-mcp",
		Module: "arbiter_engine.perfmcp",
		Serve:  []string{"serve"},
		// perfmcp serve 同样接受 --root(吸收版新增),把项目根钉死。
		Tools: []string{
			"perf.scan_c", "perf.explain_finding", "perf.measure_command", "perf.toolchain_probe",
		},
	},
}

// companionsFor 按解析到的引擎运行时实例化伙伴条目。所有路径一律绝对:
// Claude Code 拉起 stdio 服务器的 cwd 不可假设,相对 PYTHONPATH/--root 会
// 让服务器瞬退(用户侧表现为 reconnect -32000)。条目因此与 arbiter 二进制
// 同一姿态——机器本地,换机/搬仓后重跑 init 刷新。
func companionsFor(root, python string, embedded bool) []companion {
	out := make([]companion, 0, len(companionSpecs))
	for _, spec := range companionSpecs {
		spec.Command = python
		spec.Args = append([]string{"-m", spec.Module}, spec.Serve...)
		spec.Args = append(spec.Args, "--root", root)
		if embedded {
			spec.PythonPath = embeddedengine.PythonPath(root)
		}
		out = append(out, spec)
	}
	return out
}

// mergeCompanions 把伙伴服务器并入 .mcp.json。本工具生成的既有条目
// (python -m arbiter_engine.* —— 包括旧版写出的相对路径形态)随 init 刷新;
// 用户手写的外来同名条目原样保留。
func mergeCompanions(path string, companions []companion) error {
	if len(companions) == 0 {
		return nil
	}
	root, err := readJSON(path)
	if err != nil {
		return err
	}
	servers, _ := root["mcpServers"].(map[string]any)
	if servers == nil {
		servers = map[string]any{}
		root["mcpServers"] = servers
	}
	changed := false
	for _, comp := range companions {
		if existing, exists := servers[comp.Name]; exists && !isEngineCompanionEntry(existing) {
			continue
		}
		args := make([]any, 0, len(comp.Args))
		for _, arg := range comp.Args {
			args = append(args, arg)
		}
		entry := map[string]any{
			"type":    "stdio",
			"command": comp.Command,
			"args":    args,
		}
		if comp.PythonPath != "" {
			entry["env"] = map[string]any{"PYTHONPATH": comp.PythonPath}
		}
		servers[comp.Name] = entry
		changed = true
	}
	if !changed {
		return nil
	}
	return writeJSON(path, root, 0o644)
}

// isEngineCompanionEntry 识别本工具生成的伙伴条目:经引擎解释器以
// `-m arbiter_engine.<ns>` 拉起。识别为真 ⇒ init 可刷新(修复旧版相对路径
// 等缺陷);识别为假 ⇒ 外来内容,永不触碰。
func isEngineCompanionEntry(value any) bool {
	entry, ok := value.(map[string]any)
	if !ok {
		return false
	}
	args, ok := entry["args"].([]any)
	if !ok || len(args) < 2 {
		return false
	}
	first, _ := args[0].(string)
	second, _ := args[1].(string)
	return first == "-m" && strings.HasPrefix(second, "arbiter_engine.")
}

// renderDebugger 渲染诊断执行席 agent:席位凭证注入 + 伙伴服务器进
// frontmatter(mcpServers 与 tools 同步生成;embedded 模式附 PYTHONPATH)。
func renderDebugger(text, exe, key, root string, companions []companion) string {
	var servers strings.Builder
	var tools strings.Builder
	for i, comp := range companions {
		if i > 0 {
			servers.WriteString("\n")
		}
		servers.WriteString(fmt.Sprintf("  - %s:\n      type: stdio\n      command: %s\n      args: [%s]",
			comp.Name, comp.Command, strings.Join(comp.Args, ", ")))
		if comp.PythonPath != "" {
			servers.WriteString(fmt.Sprintf("\n      env:\n        PYTHONPATH: %s", comp.PythonPath))
		}
		for _, tool := range comp.Tools {
			tools.WriteString(", mcp__" + comp.Name + "__" + tool)
		}
	}
	text = renderSeat(text, exe, key, root)
	text = strings.ReplaceAll(text, "{{COMPANION_SERVERS}}", servers.String())
	return strings.ReplaceAll(text, "{{COMPANION_TOOLS}}", tools.String())
}

// verifyCompanions 在 init 期以 Claude Code 同款方式(cwd=root + 条目 env
// 覆盖继承环境)逐个拉起伙伴服务器并完成 initialize 握手:坏条目在 init
// 当场报 typed 错误,而不是会话里一个不可解释的 reconnect -32000。
func verifyCompanions(root string, companions []companion) error {
	for _, comp := range companions {
		if err := verifyCompanion(root, comp); err != nil {
			return &Error{
				Kind:    "companion_verify_failed",
				Message: comp.Name + " failed its initialize handshake (the .mcp.json entry would not connect): " + err.Error(),
				Err:     err,
			}
		}
	}
	return nil
}

func verifyCompanion(root string, comp companion) error {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, comp.Command, comp.Args...)
	cmd.Dir = root
	env := make([]string, 0, len(os.Environ())+2)
	for _, kv := range os.Environ() {
		if comp.PythonPath != "" && strings.HasPrefix(kv, "PYTHONPATH=") {
			continue
		}
		env = append(env, kv)
	}
	// 释放出的引擎树受摘要校验,字节码写入会让后续 Verify 失败。
	env = append(env, "PYTHONDONTWRITEBYTECODE=1")
	if comp.PythonPath != "" {
		env = append(env, "PYTHONPATH="+comp.PythonPath)
	}
	cmd.Env = env
	cmd.Stdin = strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}` + "\n")
	var stderr strings.Builder
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		detail := strings.TrimSpace(stderr.String())
		if detail == "" {
			detail = err.Error()
		}
		return fmt.Errorf("%s", tail(detail, 300))
	}
	if !strings.Contains(string(out), `"name":"`+comp.Name+`"`) {
		return fmt.Errorf("unexpected handshake response: %s", tail(strings.TrimSpace(string(out)), 200))
	}
	return nil
}

func tail(text string, max int) string {
	if len(text) <= max {
		return text
	}
	return "…" + text[len(text)-max:]
}

//go:embed templates/*
var templates embed.FS

type Options struct {
	NoExecutor     bool
	Remove         bool
	EmbeddedEngine bool
	Python         string
	FSKind         string
	Now            func() time.Time
	VerifyEngine   func(python, root string) (string, error)
	// VerifyCompanions 测试钩子:nil = 真实握手校验(见 verifyCompanions)。
	VerifyCompanions func(root string) error
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
	python := ResolvePython(opts.Python)
	verify := opts.VerifyEngine
	if verify == nil {
		verify = verifyEngine
	}
	// ADR-0011 解析阶梯:已安装包优先,但必须与本二进制内置引擎同版本——
	// 旧的 pip 安装包缺新接口(如 perfmcp --root),会在会话里以 -32000
	// 之类的形态炸开;版本不符即视为未安装,自动回退内置引擎。
	// 仅当 python3 本身缺席/不可用时才失败 —— 这是唯一的系统前置条件。
	expectedVersion, err := embeddedengine.Version()
	if err != nil {
		return "", err
	}
	embedded := opts.EmbeddedEngine || isEmbeddedDeployment(root)
	var embeddedDigest string
	var engineVersion string
	var staleInstalled string
	if !embedded {
		switch version, probeErr := verify(python, root); {
		case probeErr != nil:
			embedded = true
		case version != expectedVersion:
			staleInstalled = version
			embedded = true
		default:
			engineVersion = version
		}
	}
	if embedded {
		manifest, err := embeddedengine.Unpack(root)
		if err != nil {
			return "", err
		}
		embeddedDigest = manifest.Digest
		version, err := verify(python, root)
		if err != nil {
			return "", &Error{Kind: "engine_verify_failed", Message: "engine verification failed — the one system prerequisite is python3 (>= 3.9); install it and re-run arbiter init", Err: err}
		}
		engineVersion = version
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
	// FORMAT.md 与起手棋谱是 arbiter 自带、deploy 生成的资产(与 .claude/agents
	// 同):每次 init 刷新到最新模板,这样升级 arbiter 后重跑 init 就能拿到新版
	// 棋谱(此前 write-if-missing 让 shipped 棋谱的更新永远到不了既有仓库)。
	// 用户定制应另起新名(AddPlayBook)——那些文件不在 baseOpenings 列表、
	// init 永不触碰。注意 config.yml / recipes.yaml 才是用户状态(recipes.yaml
	// 存着派生配方),下面仍保持 write-if-missing,绝不可在此一并刷新。
	if err := atomicWrite(filepath.Join(root, fileFormat), []byte(mustTemplate("templates/FORMAT.md")), 0o644); err != nil {
		return "", err
	}
	for _, opening := range baseOpenings {
		if err := atomicWrite(filepath.Join(root, dirPlaybook, opening.file), []byte(mustTemplate(opening.template)), 0o644); err != nil {
			return "", err
		}
	}
	if err := writeIfMissing(filepath.Join(root, fileConfig), defaultConfig(), 0o644); err != nil {
		return "", err
	}
	if err := writeIfMissing(filepath.Join(root, fileRecipes), defaultRecipes(), 0o644); err != nil {
		return "", err
	}
	if err := writeEngines(filepath.Join(root, fileEngines), python, engineVersion, now(opts), embedded, embeddedDigest); err != nil {
		return "", err
	}
	replacedMCP, err := mergeMCP(filepath.Join(root, fileMCP), exe, root)
	if err != nil {
		return "", err
	}
	companions := companionsFor(root, python, embedded)
	if err := mergeCompanions(filepath.Join(root, fileMCP), companions); err != nil {
		return "", err
	}
	if opts.VerifyCompanions != nil {
		if err := opts.VerifyCompanions(root); err != nil {
			return "", err
		}
	} else if err := verifyCompanions(root, companions); err != nil {
		return "", err
	}
	curator := renderSeat(mustTemplate("templates/arbiter-curator.md"), exe, key, root)
	if err := atomicWrite(filepath.Join(root, fileCurator), []byte(curator), 0o600); err != nil {
		return "", err
	}
	if !opts.NoExecutor {
		for _, agent := range executorAgents {
			rendered := renderSeat(mustTemplate(agent.template), exe, key, root)
			if err := atomicWrite(filepath.Join(root, agent.file), []byte(rendered), 0o600); err != nil {
				return "", err
			}
		}
		debugger := renderDebugger(mustTemplate("templates/arbiter-debugger.md"), exe, key, root, companions)
		if err := atomicWrite(filepath.Join(root, fileDebugger), []byte(debugger), 0o600); err != nil {
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
	if err := mergeSettings(filepath.Join(root, fileSettings), exe, root, embedded); err != nil {
		return "", err
	}
	if err := appendGitignore(filepath.Join(root, fileGitignore), embedded); err != nil {
		return "", err
	}
	return guidance(replacedMCP, opts.NoExecutor, embedded, staleInstalled, expectedVersion), nil
}

// baseOpenings:ADR-0012 起手棋谱(templates/openings/,命名规约 CI 校验)
// + 设计钦定的 intro 系棋谱。旧 debug/feature/review 已被裁判原生的
// fix-reported-bug / build-feature / hunt-latent-bugs 取代并退役。
var baseOpenings = []struct {
	file     string
	template string
}{
	{"build-feature.md", "templates/openings/build-feature.md"},
	{"fix-reported-bug.md", "templates/openings/fix-reported-bug.md"},
	{"fix-slow-path.md", "templates/openings/fix-slow-path.md"},
	{"hunt-latent-bugs.md", "templates/openings/hunt-latent-bugs.md"},
	{"freeplay.md", "templates/freeplay.md"},
	{"gold-digger.md", "templates/gold-digger.md"},
	{"recipe-derivation.md", "templates/recipe-derivation.md"},
	{"regression-triage.md", "templates/regression-triage.md"},
}

// executorAgents are the executor-seat subagents deployed with the executor
// (and skipped together under --no-executor): all of them speak to
// `arbiter serve executor` with the injected seat key.
var executorAgents = []struct {
	file     string
	template string
}{
	{fileExecutor, "templates/arbiter-executor.md"},
	{fileImplementer, "templates/arbiter-implementer.md"},
	{fileTestAuthor, "templates/arbiter-test-author.md"},
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

func mergeMCP(path, exe, repoRoot string) (bool, error) {
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
		"args":    []any{"serve", "player", "--root", repoRoot},
	}
	return replaced, writeJSON(path, root, 0o644)
}

func mergeSettings(path, exe, repoRoot string, embedded bool) error {
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
	mergeStopHook(root, exe, repoRoot)
	return writeJSON(path, root, 0o644)
}

// isArbiterStopHook reports whether a Stop hook command is arbiter-owned:
// either the exact current command, or a provably dead arbiter entry — a
// command ending in "hook stop" whose first field has basename "arbiter" and
// no longer resolves to an existing file on disk. The liveness check is what
// lets init and remove reclaim hooks left behind by moved or rebuilt arbiter
// binaries while guaranteeing a live foreign binary that happens to be named
// "arbiter" is never hijacked.
func isArbiterStopHook(command, exe string) bool {
	fields := strings.Fields(command)
	// 归一:剥掉尾部 "--root <dir>"(带根形态),其余按 legacy 规则识别。
	if len(fields) >= 4 && fields[len(fields)-2] == "--root" {
		fields = fields[:len(fields)-2]
	}
	if len(fields) < 3 || fields[len(fields)-2] != "hook" || fields[len(fields)-1] != "stop" {
		return false
	}
	if fields[0] == exe {
		return true
	}
	if filepath.Base(fields[0]) != "arbiter" {
		return false
	}
	return !binaryExists(fields[0])
}

// binaryExists reports whether a hook command's first token still resolves to
// a file on disk: os.Stat for path-shaped tokens, exec.LookPath for bare
// names. A token that no longer resolves marks the hook as dead/stale.
func binaryExists(token string) bool {
	if strings.ContainsRune(token, '/') || strings.ContainsRune(token, os.PathSeparator) {
		_, err := os.Stat(token)
		return err == nil
	}
	_, err := exec.LookPath(token)
	return err == nil
}

// mergeStopHook rewrites any arbiter-owned Stop hook to the current command
// (so stale entries from moved/rebuilt binaries do not accumulate) and drops
// duplicates; if no arbiter-owned entry exists it appends a fresh one.
func mergeStopHook(root map[string]any, exe, repoRoot string) {
	hooks, _ := root["hooks"].(map[string]any)
	if hooks == nil {
		hooks = map[string]any{}
		root["hooks"] = hooks
	}
	stops, _ := hooks["Stop"].([]any)
	cmd := exe + " hook stop --root " + repoRoot
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
	mergeGuardHook(hooks, exe, repoRoot)
	mergeSubagentStopHook(hooks, exe, repoRoot)
}

// subagentStopMatcher 限定门控只对执行席位子代理生效:curator 不交
// SubmitTask,匹配不到它就永远不会被误拦。
const subagentStopMatcher = "arbiter-executor|arbiter-implementer|arbiter-test-author|arbiter-debugger"

// mergeSubagentStopHook 注册 SubagentStop 门控(幂等):被派发 task 未交
// SubmitTask 的执行子代理不准收工。识别本工具的既有条目就刷新命令与
// matcher,否则追加;外来条目原样保留。门控本体见 internal/match
// (SubagentStopGate)与 `arbiter hook subagent-stop`。
func mergeSubagentStopHook(hooks map[string]any, exe, repoRoot string) {
	entries, _ := hooks["SubagentStop"].([]any)
	cmd := exe + " hook subagent-stop --root " + repoRoot
	claimed := false
	var kept []any
	for _, entry := range entries {
		em, ok := entry.(map[string]any)
		if !ok {
			kept = append(kept, entry)
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
			if !isArbiterSubagentStopHook(c, exe) {
				keptHooks = append(keptHooks, h)
				continue
			}
			if claimed {
				continue
			}
			hm["command"] = cmd
			em["matcher"] = subagentStopMatcher
			claimed = true
			keptHooks = append(keptHooks, hm)
		}
		if len(inner) > 0 && len(keptHooks) == 0 {
			continue
		}
		if len(inner) > 0 {
			em["hooks"] = keptHooks
		}
		kept = append(kept, em)
	}
	if !claimed {
		kept = append(kept, map[string]any{
			"matcher": subagentStopMatcher,
			"hooks":   []any{map[string]any{"type": "command", "command": cmd, "timeout": 10}},
		})
	}
	hooks["SubagentStop"] = kept
}

func isArbiterSubagentStopHook(command, exe string) bool {
	fields := strings.Fields(command)
	if len(fields) >= 4 && fields[len(fields)-2] == "--root" {
		fields = fields[:len(fields)-2]
	}
	if len(fields) < 3 || fields[len(fields)-2] != "hook" || fields[len(fields)-1] != "subagent-stop" {
		return false
	}
	if fields[0] == exe {
		return true
	}
	if filepath.Base(fields[0]) != "arbiter" {
		return false
	}
	return !binaryExists(fields[0])
}

// mergeGuardHook 注册 PreToolUse 门控(幂等):识别本工具的既有条目
// (含 legacy 无根形态)就刷新命令与 matcher,否则追加;外来 PreToolUse
// 条目原样保留。门控本体见 internal/guard 与 `arbiter hook guard`。
func mergeGuardHook(hooks map[string]any, exe, repoRoot string) {
	pres, _ := hooks["PreToolUse"].([]any)
	cmd := exe + " hook guard --root " + repoRoot
	matcher := "Bash|Read|Edit|Write|NotebookEdit|Glob|Grep"
	claimed := false
	var kept []any
	for _, entry := range pres {
		em, ok := entry.(map[string]any)
		if !ok {
			kept = append(kept, entry)
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
			if !isArbiterGuardHook(c, exe) {
				keptHooks = append(keptHooks, h)
				continue
			}
			if claimed {
				continue
			}
			hm["command"] = cmd
			em["matcher"] = matcher
			claimed = true
			keptHooks = append(keptHooks, hm)
		}
		if len(inner) > 0 && len(keptHooks) == 0 {
			continue
		}
		if len(inner) > 0 {
			em["hooks"] = keptHooks
		}
		kept = append(kept, em)
	}
	if !claimed {
		kept = append(kept, map[string]any{
			"matcher": matcher,
			"hooks":   []any{map[string]any{"type": "command", "command": cmd, "timeout": 10}},
		})
	}
	hooks["PreToolUse"] = kept
}

func isArbiterGuardHook(command, exe string) bool {
	fields := strings.Fields(command)
	if len(fields) >= 4 && fields[len(fields)-2] == "--root" {
		fields = fields[:len(fields)-2]
	}
	if len(fields) < 3 || fields[len(fields)-2] != "hook" || fields[len(fields)-1] != "guard" {
		return false
	}
	if fields[0] == exe {
		return true
	}
	if filepath.Base(fields[0]) != "arbiter" {
		return false
	}
	return !binaryExists(fields[0])
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
		fileEngines, fileSeatKey, fileCurator, fileExecutor, fileTestAuthor, fileImplementer, fileDebugger,
		fileSkill, fileSkillIntro, fileSkillCreate, fileFormat, fileConfig, fileRecipes,
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
	// 本工具生成的伙伴条目(python -m arbiter_engine.*)随 --remove 一并撤除;
	// 用户手写的同名外来条目不在此列(isEngineCompanionEntry 区分)。
	for _, spec := range companionSpecs {
		if isEngineCompanionEntry(servers[spec.Name]) {
			delete(servers, spec.Name)
		}
	}
	return writeJSON(path, root, 0o644)
}

func isArbiterServer(value any, exe string) bool {
	server, ok := value.(map[string]any)
	if !ok {
		return false
	}
	args, _ := server["args"].([]any)
	if server["command"] != exe || len(args) < 2 || args[0] != "serve" || args[1] != "player" {
		return false
	}
	return len(args) == 2 || (len(args) == 4 && args[2] == "--root")
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
	removeGuardHook(root, exe)
	removeSubagentStopHook(root, exe)
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

// renderSeat 在 render 之上替换 {{ARBITER_ROOT}}:席位服务器条目必须携带
// 显式仓根 —— 主会话与子代理拉起 MCP 服务器的 cwd 可能不同,cwd 推导的
// 仓根会让 curator 写出的对局对 player 不可见("no active match")。
func renderSeat(text, exe, key, root string) string {
	return strings.ReplaceAll(render(text, exe, key), "{{ARBITER_ROOT}}", root)
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

func guidance(replacedMCP, noExecutor, embedded bool, staleInstalled, expectedVersion string) string {
	msg := "arbiter 已部署。已写入引擎校验、席位凭证、Claude agents、skills、MCP 与 Stop hook 配置。\n"
	if staleInstalled != "" {
		msg += "提示:检测到已安装的 arbiter-engine v" + staleInstalled + " 与本二进制 (v" + expectedVersion + ") 不匹配,已忽略并改用内置引擎;可 pip uninstall arbiter-engine 消除此提示。\n"
	}
	if embedded {
		msg += "引擎:已从二进制释放到 .arbiter/engine(零额外安装;Edit/Write 拒绝规则 + 摘要校验已就位;升级 arbiter 后重跑 init 自动刷新)。\n"
	} else {
		msg += "引擎:使用已安装的 arbiter-engine 包。\n"
	}
	msg += "起手棋谱已刷新到最新:.arbiter/playbook/(fix-reported-bug, hunt-latent-bugs, build-feature, fix-slow-path + freeplay, gold-digger, recipe-derivation, regression-triage;arbiter 自带棋谱每次 init 覆盖刷新,定制请另起新名;用户自带棋谱与 recipes.yaml 不动)。\n"
	msg += "伙伴诊断服务器已接线(ADR-0010):gdb-mcp, perf-mcp(既有同名 .mcp.json 条目保留);崩溃/内存破坏/性能类任务派发给 arbiter-debugger 子代理。\n"
	if noExecutor {
		msg += "提示:--no-executor 已跳过 executor agents(含 arbiter-debugger)。\n"
	}
	if replacedMCP {
		msg += "提示:.mcp.json 中既有 arbiter 服务器指向不同命令,已覆盖为当前二进制。\n"
	}
	msg += "下一步:在本仓库打开或重启 Claude Code,使其加载新写入的 skills 与 MCP 服务器(斜杠命令需会话拾取 .claude/skills/ 后才出现)。\n"
	return msg
}

// defaultConfig must stay parseable by the engine's strict config parser
// (engine/arbiter_engine/config/__init__.py), which only allows
// facts.{extractor,incremental,index_on_build} and nests key_flags at
// facts.index_on_build.key_flags.
func defaultConfig() string {
	return "# Arbiter engine config.\nfacts:\n  index_on_build:\n    key_flags: []\n"
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
	// PYTHONDONTWRITEBYTECODE keeps this probe from writing __pycache__/*.pyc
	// into the engine tree; for the embedded engine that bytecode would change
	// the freshly-unpacked tree whose digest init just recorded.
	cmd.Env = append(os.Environ(), "PYTHONDONTWRITEBYTECODE=1")
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
		// Read(...) 只约束 Read 工具;Edit/Write 也要拒(Bash/Grep/Glob 的
		// 兜底由 PreToolUse 门控 `arbiter hook guard` 负责)。
		"Edit(.arbiter/playbook/**)",
		"Write(.arbiter/playbook/**)",
		"Edit(.arbiter/match/**)",
		"Write(.arbiter/match/**)",
		"Edit(.claude/agents/arbiter-*.md)",
		"Write(.claude/agents/arbiter-*.md)",
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
		".claude/agents/arbiter-implementer.md",
		".claude/agents/arbiter-test-author.md",
		".claude/agents/arbiter-debugger.md",
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

// removeSubagentStopHook 撤除本工具的 SubagentStop 门控条目;外来条目保留。
func removeSubagentStopHook(root map[string]any, exe string) {
	hooks, _ := root["hooks"].(map[string]any)
	if hooks == nil {
		return
	}
	entries, _ := hooks["SubagentStop"].([]any)
	var kept []any
	for _, entry := range entries {
		em, ok := entry.(map[string]any)
		if !ok {
			kept = append(kept, entry)
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
			if isArbiterSubagentStopHook(c, exe) {
				continue
			}
			keptHooks = append(keptHooks, hm)
		}
		if len(inner) > 0 && len(keptHooks) == 0 {
			continue
		}
		if len(inner) > 0 {
			em["hooks"] = keptHooks
		}
		kept = append(kept, em)
	}
	if len(kept) == 0 {
		delete(hooks, "SubagentStop")
	} else {
		hooks["SubagentStop"] = kept
	}
}

// removeGuardHook 撤除本工具的 PreToolUse 门控条目;外来条目保留。
func removeGuardHook(root map[string]any, exe string) {
	hooks, _ := root["hooks"].(map[string]any)
	if hooks == nil {
		return
	}
	pres, _ := hooks["PreToolUse"].([]any)
	var kept []any
	for _, entry := range pres {
		em, ok := entry.(map[string]any)
		if !ok {
			kept = append(kept, entry)
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
			if isArbiterGuardHook(c, exe) {
				continue
			}
			keptHooks = append(keptHooks, hm)
		}
		if len(inner) > 0 && len(keptHooks) == 0 {
			continue
		}
		if len(inner) > 0 {
			em["hooks"] = keptHooks
		}
		kept = append(kept, em)
	}
	if len(kept) == 0 {
		delete(hooks, "PreToolUse")
	} else {
		hooks["PreToolUse"] = kept
	}
}
