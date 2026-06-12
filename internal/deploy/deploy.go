package deploy

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"

	arbiter "github.com/HoldThatThgt/arbiter"
	"github.com/HoldThatThgt/arbiter/internal/playbook"
)

const (
	dirPlaybook     = ".arbiter/match/playbook"
	dirRun          = ".arbiter/match/run"
	dirLog          = ".arbiter/match/log"
	fileFormat      = ".arbiter/match/FORMAT.md"
	fileSeatKey     = ".arbiter/match/run/seat.key"
	fileMCP         = ".mcp.json"
	fileSettings    = ".claude/settings.json"
	fileCurator     = ".claude/agents/arbiter-curator.md"
	fileSkill       = ".claude/skills/arbiter-play/SKILL.md"
	fileSkillCreate = ".claude/skills/playbook-create/SKILL.md"
	fileGitignore   = ".gitignore"
	fileExecutor    = ".claude/agents/arbiter-executor.md"
	fileDebugger    = ".claude/agents/arbiter-debugger.md"

	dirEmbeddedEngine  = ".arbiter/engine"
	fileEngineDigest   = ".arbiter/engine/.digest"
	embeddedEngineRoot = "engine/arbiter_engine" // arbiter.EngineFS 内的根
)

// engineRuntime 描述 init 解析到的引擎运行时(ADR-0011 解析阶梯):
// installed(pip 安装包,首选)→ embedded(从二进制释放到 .arbiter/engine)。
// Mode 为空表示主机连 python3 都没有 —— 唯一的系统前置条件。
type engineRuntime struct {
	Mode       string
	Python     string
	PythonPath string // embedded 模式下伙伴条目所需的 PYTHONPATH;installed 为空
}

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
		Serve:  []string{"serve", "--root", "."},
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
		Tools: []string{
			"perf.scan_c", "perf.explain_finding", "perf.measure_command", "perf.toolchain_probe",
		},
	},
}

// resolveEngine 实现 ADR-0011 的解析阶梯:已安装包优先,否则把内置引擎
// 释放到 .arbiter/engine。只有主机缺 python3 时才没有引擎(Mode 为空)。
func resolveEngine(root string) (engineRuntime, error) {
	python, err := exec.LookPath("python3")
	if err != nil {
		return engineRuntime{}, nil
	}
	if abs, err := filepath.Abs(python); err == nil {
		python = abs
	}
	if resolved, err := filepath.EvalSymlinks(python); err == nil {
		python = resolved
	}
	if probeInstalledEngine(python) {
		return engineRuntime{Mode: "installed", Python: python}, nil
	}
	if err := materializeEmbeddedEngine(root); err != nil {
		return engineRuntime{}, err
	}
	return engineRuntime{Mode: "embedded", Python: python, PythonPath: dirEmbeddedEngine}, nil
}

// probeInstalledEngine 以洗净 PYTHONPATH 的环境探测 pip 安装包,避免开发
// shell 的临时 PYTHONPATH 把"可导入"误判为"已安装"(.mcp.json 条目不会
// 继承当前 shell 环境)。
func probeInstalledEngine(python string) bool {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	probe := exec.CommandContext(ctx, python, "-c", "import arbiter_engine.gdbmcp, arbiter_engine.perfmcp")
	probe.Env = envWithout(os.Environ(), "PYTHONPATH")
	return probe.Run() == nil
}

func envWithout(env []string, key string) []string {
	prefix := key + "="
	out := make([]string, 0, len(env))
	for _, kv := range env {
		if strings.HasPrefix(kv, prefix) {
			continue
		}
		out = append(out, kv)
	}
	return out
}

// materializeEmbeddedEngine 把二进制内置的引擎树释放到 .arbiter/engine
// (仅 *.py,跳过 __pycache__),以内容摘要幂等:摘要一致即零写入。
// 配套防护(ADR-0007/0011):settings 写入 Edit/Write 拒绝规则,目录入
// .gitignore;评估器 spawn 期摘要校验随 engineclient 接线(M4/M5)落地。
func materializeEmbeddedEngine(root string) error {
	files, digest, err := embeddedEngineFiles()
	if err != nil {
		return err
	}
	digestPath := filepath.Join(root, fileEngineDigest)
	if existing, err := os.ReadFile(digestPath); err == nil && strings.TrimSpace(string(existing)) == digest {
		return nil
	}
	var paths []string
	for rel := range files {
		paths = append(paths, rel)
	}
	sort.Strings(paths)
	for _, rel := range paths {
		if err := atomicWrite(filepath.Join(root, dirEmbeddedEngine, filepath.FromSlash(rel)), files[rel], 0o644); err != nil {
			return err
		}
	}
	return atomicWrite(digestPath, []byte(digest+"\n"), 0o644)
}

func embeddedEngineFiles() (map[string][]byte, string, error) {
	files := map[string][]byte{}
	var paths []string
	err := fs.WalkDir(arbiter.EngineFS, embeddedEngineRoot, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.IsDir() {
			if entry.Name() == "__pycache__" {
				return fs.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".py") {
			return nil
		}
		data, err := arbiter.EngineFS.ReadFile(path)
		if err != nil {
			return err
		}
		rel := strings.TrimPrefix(path, "engine/")
		files[rel] = data
		paths = append(paths, rel)
		return nil
	})
	if err != nil {
		return nil, "", err
	}
	sort.Strings(paths)
	digest := sha256.New()
	for _, rel := range paths {
		digest.Write([]byte(rel))
		digest.Write([]byte{0})
		digest.Write(files[rel])
		digest.Write([]byte{0})
	}
	return files, hex.EncodeToString(digest.Sum(nil)), nil
}

func companionsFor(rt engineRuntime) []companion {
	if rt.Mode == "" {
		return nil
	}
	out := make([]companion, 0, len(companionSpecs))
	for _, spec := range companionSpecs {
		spec.Command = rt.Python
		spec.Args = append([]string{"-m", spec.Module}, spec.Serve...)
		spec.PythonPath = rt.PythonPath
		out = append(out, spec)
	}
	return out
}

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

	for _, dir := range []string{dirPlaybook, dirRun, dirLog, ".claude/agents", ".claude/skills/arbiter-play", ".claude/skills/playbook-create"} {
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
	openings, err := deliverOpenings(root)
	if err != nil {
		return "", err
	}
	replacedMCP, err := mergeMCP(filepath.Join(root, fileMCP), exe)
	if err != nil {
		return "", err
	}
	runtime, err := resolveEngine(root)
	if err != nil {
		return "", err
	}
	companions := companionsFor(runtime)
	if err := mergeCompanions(filepath.Join(root, fileMCP), companions); err != nil {
		return "", err
	}
	curator := render(mustTemplate("templates/arbiter-curator.md"), exe, key)
	if err := atomicWrite(filepath.Join(root, fileCurator), []byte(curator), 0o600); err != nil {
		return "", err
	}
	if len(companions) > 0 {
		debugger := renderDebugger(mustTemplate("templates/arbiter-debugger.md"), exe, key, companions)
		if err := atomicWrite(filepath.Join(root, fileDebugger), []byte(debugger), 0o600); err != nil {
			return "", err
		}
	}
	skill := mustTemplate("templates/arbiter-play.md")
	if err := atomicWrite(filepath.Join(root, fileSkill), []byte(skill), 0o644); err != nil {
		return "", err
	}
	if err := atomicWrite(filepath.Join(root, fileSkillCreate), []byte(mustTemplate("templates/playbook-create.md")), 0o644); err != nil {
		return "", err
	}
	embedded := runtime.Mode == "embedded"
	if err := mergeSettings(filepath.Join(root, fileSettings), exe, len(companions) > 0, embedded); err != nil {
		return "", err
	}
	if err := appendGitignore(filepath.Join(root, fileGitignore), len(companions) > 0, embedded); err != nil {
		return "", err
	}
	return guidance(exe, key, replacedMCP, companions, runtime, openings), nil
}

// deliverOpenings 把内置起手棋谱写进 .arbiter/match/playbook,write-if-missing:
// 既有同名文件视为用户内容,绝不覆盖。返回本次实际写入的棋谱名。
func deliverOpenings(root string) ([]string, error) {
	entries, err := templates.ReadDir("templates/openings")
	if err != nil {
		return nil, err
	}
	var delivered []string
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".md") {
			continue
		}
		target := filepath.Join(root, dirPlaybook, entry.Name())
		if _, err := os.Stat(target); err == nil {
			continue
		}
		if err := writeIfMissing(target, mustTemplate("templates/openings/"+entry.Name()), 0o644); err != nil {
			return nil, err
		}
		delivered = append(delivered, strings.TrimSuffix(entry.Name(), ".md"))
	}
	sort.Strings(delivered)
	return delivered, nil
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

// mergeCompanions 把探测到的伙伴服务器并入 .mcp.json,add-if-missing:
// 既有同名条目是外来内容(可能由 gdb-mcp/perf-mcp 自带 init 写入),原样保留。
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
		if _, exists := servers[comp.Name]; exists {
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

// renderDebugger 渲染诊断执行席 agent:席位凭证注入 + 伙伴服务器进
// frontmatter(mcpServers 与 tools 同步生成;embedded 模式附 PYTHONPATH)。
func renderDebugger(text, exe, key string, companions []companion) string {
	var servers strings.Builder
	var tools strings.Builder
	for i, comp := range companions {
		if i > 0 {
			servers.WriteString("\n")
		}
		servers.WriteString(fmt.Sprintf("  %s:\n    type: stdio\n    command: %s\n    args: [%s]",
			comp.Name, comp.Command, strings.Join(comp.Args, ", ")))
		if comp.PythonPath != "" {
			servers.WriteString(fmt.Sprintf("\n    env:\n      PYTHONPATH: %s", comp.PythonPath))
		}
		for _, tool := range comp.Tools {
			tools.WriteString(", mcp__" + comp.Name + "__" + tool)
		}
	}
	text = render(text, exe, key)
	text = strings.ReplaceAll(text, "{{COMPANION_SERVERS}}", servers.String())
	return strings.ReplaceAll(text, "{{COMPANION_TOOLS}}", tools.String())
}

func mergeSettings(path, exe string, withDebugger, embedded bool) error {
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
	items := []string{
		"Read(.arbiter/match/playbook/**)",
		"Read(.arbiter/match/run/**)",
		"Read(.claude/agents/arbiter-curator.md)",
		"Read(.claude/agents/arbiter-executor.md)",
	}
	if withDebugger {
		items = append(items, "Read(.claude/agents/arbiter-debugger.md)")
	}
	if embedded {
		// ADR-0007/0011:释放出的引擎树不可被模型悄悄改写。
		items = append(items, "Edit(.arbiter/engine/**)", "Write(.arbiter/engine/**)")
	}
	for _, item := range items {
		if !hasLineValue(deny, item) {
			deny = append(deny, item)
		}
	}
	perms["deny"] = deny
	mergeStopHook(root, exe)
	return writeJSON(path, root, 0o644)
}

// mergeStopHook 注册停止门控 hook(幂等):命令尾词为 "hook stop" 的条目视为
// Arbiter 所有,刷新其二进制路径;不存在则追加,既有其他 hook 原样保留。
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

func appendGitignore(path string, withDebugger, embedded bool) error {
	var lines []string
	if data, err := os.ReadFile(path); err == nil {
		text := strings.TrimSuffix(string(data), "\n")
		if text != "" {
			lines = strings.Split(text, "\n")
		}
	}
	items := []string{
		".arbiter/match/run/",
		".arbiter/match/log/",
		".arbiter/match/status.json",
		".claude/agents/arbiter-curator.md",
	}
	if withDebugger {
		items = append(items, ".claude/agents/arbiter-debugger.md")
	}
	if embedded {
		items = append(items, ".arbiter/engine/")
	}
	for _, item := range items {
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

func guidance(exe, key string, replacedMCP bool, companions []companion, runtime engineRuntime, openings []string) string {
	openingsLine := "起手棋谱已就位:.arbiter/match/playbook/{fix-reported-bug, hunt-latent-bugs, build-feature, fix-slow-path}(既有文件未覆盖)"
	if len(openings) == 0 {
		openingsLine = "起手棋谱:.arbiter/match/playbook/ 下同名文件均已存在,未做改动"
	}
	msg := openingsLine + fmt.Sprintf(`
arbiter 已部署。剩余一件事:
提供执行席位 agent: .claude/agents/arbiter-executor.md,模板如下
   ┌─────────────────────────────────────────────
   │ ---
   │ name: arbiter-executor
   │ description: 执行 arbiter 任务并提交可验证结果
   │ tools: Bash, Read, Write, Edit, Glob, Grep,
   │   mcp__arbiter-executor__SubmitTask, mcp__arbiter-executor__ReviewTask
   │ mcpServers:
   │   arbiter-executor:
   │     type: stdio
   │     command: %s
   │     args: [serve, executor]
   │     env:
   │       ARBITER_SEAT_KEY: %s
   │ ---
   │ 你是任务执行者。完成提示中的任务后,必须调用 SubmitTask:
   │ task_id 取提示中的编号,summary 一句话概括结果(进全局任务清单,
   │ 供棋手通览与复盘),report 写明做了什么与证据,result 填能
   │ 独立验证完成的谓词——shell 命令(退出码 0 即通过)或 mcp 调用
   │ (server/tool/arguments,应答非错误即通过;可附 expect 子句
   │ [{path,op:eq|ne|ge|le|exists,value}] 对照 structuredContent
   │ 类型化字段,全部成立才通过)。验证耗时长或输出大时,
   │ 可在 result 中附 timeout_s(默认 600)/ output_lines(默认 256)。
   │ 提交后可用 ReviewTask 查看判定;失败可修复后重交。
   └─────────────────────────────────────────────
   (模板已嵌入本机席位凭证;建议把该文件加入 .gitignore,换机后重跑 arbiter init)
完成后打开 claude 即可使用(试试: /arbiter-play 修复 <某个 bug>)。
起手棋谱之外的流程用 /playbook-create 起草注册(命名与谓词规范见 FORMAT.md)。
已注册 Stop 门控:对局进行中模型无法自行停止(用户中断不受影响)。
`, exe, key)
	if replacedMCP {
		msg += "提示:.mcp.json 中既有 arbiter 服务器指向不同命令,已覆盖为当前二进制。\n"
	}
	switch runtime.Mode {
	case "installed":
		msg += "引擎:使用已安装的 arbiter-engine 包(" + runtime.Python + ")。\n"
	case "embedded":
		msg += "引擎:已从二进制释放到 .arbiter/engine(零额外安装;已加入 .gitignore 并设 Edit/Write 拒绝规则;升级 arbiter 后重跑 init 自动刷新)。\n"
	}
	if len(companions) > 0 {
		var names []string
		for _, comp := range companions {
			names = append(names, comp.Name)
		}
		msg += fmt.Sprintf(`内置伙伴诊断服务器已接线(ADR-0010):%s
- .mcp.json 已并入对应条目(经引擎解释器拉起;既有同名条目原样保留)
- 已写入诊断执行席 .claude/agents/arbiter-debugger.md(凭证已注入,已加入 .gitignore)
  崩溃/内存破坏/性能类任务请派发给 arbiter-debugger 子代理。
`, strings.Join(names, ", "))
	} else {
		msg += "唯一缺失的系统前置:python3(>= 3.9)。装好后重跑 arbiter init,引擎与 gdb-mcp / perf-mcp 诊断执行席即自动就位 —— 无任何 pip 步骤。\n"
	}
	return msg
}
