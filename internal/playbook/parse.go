package playbook

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode"

	"gopkg.in/yaml.v3"
)

const (
	tokenStep       = "[STEP]"
	tokenStepJob    = "[StepJob]"
	tokenCheckList  = "[CheckList]"
	tokenBranch     = "[Branch]"
	tokenSetGoal    = "[SetGoal]"
	tokenVerify     = "[Verify]"
	tokenSubmit     = "[Submit]"
	tokenCheckpoint = "[Checkpoint]"
	tokenGotcha     = "[Gotcha]"
	tokenListItem   = "-"

	sectionNone       = ""
	sectionJob        = "job"
	sectionList       = "checklist"
	sectionJump       = "branch"
	sectionGoal       = "goal"
	sectionVerify     = "verify"
	sectionGotcha     = "gotcha"
	sectionCheckpoint = "checkpoint"
)

type frontmatter struct {
	Name         string   `yaml:"name"`
	Description  string   `yaml:"description"`
	MaxSteps     int      `yaml:"max_steps"`
	Capabilities []string `yaml:"capabilities"`
	VerifyPolicy string   `yaml:"verify_policy"`
}

type stepBuilder struct {
	step            Step
	line            int
	seenJob         bool
	seenList        bool
	seenBranch      bool
	seenCheckpoint  bool
	jobLines        []string
	checkpointLines []string
	branchSeen      map[string]int
}

func ParseFile(path string) (Playbook, []Issue) {
	info, err := os.Stat(path)
	if err != nil {
		return Playbook{}, []Issue{{File: filepath.Base(path), Code: IssueBadFrontmatter, Detail: err.Error()}}
	}
	if info.Size() > MaxPlaybookBytes {
		return Playbook{}, []Issue{{File: filepath.Base(path), Code: IssueOversize, Detail: fmt.Sprintf("%d bytes", info.Size())}}
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return Playbook{}, []Issue{{File: filepath.Base(path), Code: IssueBadFrontmatter, Detail: err.Error()}}
	}
	return ParseBytes(filepath.Base(path), data)
}

func ParseBytes(file string, data []byte) (Playbook, []Issue) {
	if len(data) > MaxPlaybookBytes {
		return Playbook{}, []Issue{{File: file, Code: IssueOversize, Detail: fmt.Sprintf("%d bytes", len(data))}}
	}
	lines := splitLines(data)
	meta, bodyStart, issues := parseHeader(file, lines)
	if len(issues) > 0 {
		return Playbook{}, issues
	}

	book := Playbook{
		Name:         strings.TrimSpace(meta.Name),
		Description:  strings.TrimSpace(meta.Description),
		MaxSteps:     meta.MaxSteps,
		Capabilities: normalizeCapabilities(meta.Capabilities),
		VerifyPolicy: strings.TrimSpace(meta.VerifyPolicy),
		Verify:       map[string]ResultSpec{},
		Steps:        map[string]Step{},
	}
	var current *stepBuilder
	section := sectionNone
	parseIssues := validateCapabilities(file, book.Capabilities)
	switch book.VerifyPolicy {
	case "", "open", "named":
	default:
		parseIssues = append(parseIssues, Issue{File: file, Line: 1, Code: IssueBadFrontmatter, Detail: "verify_policy must be open or named"})
	}
	var goal *ResultSpec
	goalKeys := map[string]bool{}
	var verifyName string
	var verifyLine int
	var verifySpec *ResultSpec
	verifyKeys := map[string]bool{}

	finish := func() {
		if current == nil {
			return
		}
		if !current.seenJob {
			parseIssues = append(parseIssues, Issue{File: file, Line: current.line, Code: IssueMissingSection, Detail: tokenStepJob})
		}
		// 每个步骤恰好一种裁决面:[CheckList](执行任务)或 [Checkpoint](人工
		// 确认关卡)。两者皆缺或并存都是错误。
		switch {
		case current.seenList && current.seenCheckpoint:
			parseIssues = append(parseIssues, Issue{File: file, Line: current.line, Code: IssueBadCheckpoint, Detail: "step has both [CheckList] and [Checkpoint]; use exactly one"})
		case !current.seenList && !current.seenCheckpoint:
			parseIssues = append(parseIssues, Issue{File: file, Line: current.line, Code: IssueMissingSection, Detail: tokenCheckList})
		}
		if !current.seenBranch {
			parseIssues = append(parseIssues, Issue{File: file, Line: current.line, Code: IssueMissingSection, Detail: tokenBranch})
		}
		current.step.Job = strings.Join(current.jobLines, "\n")
		if current.seenCheckpoint {
			current.step.Checkpoint = strings.TrimSpace(strings.Join(current.checkpointLines, "\n"))
			if current.step.Checkpoint == "" {
				parseIssues = append(parseIssues, Issue{File: file, Line: current.line, Code: IssueBadCheckpoint, Detail: "[Checkpoint] question is empty"})
			}
		}
		book.Steps[current.step.ID] = current.step
		book.order = append(book.order, current.step.ID)
		if book.Entry == "" {
			book.Entry = current.step.ID
		}
		current = nil
		section = sectionNone
	}
	finishVerify := func() {
		if verifySpec == nil {
			return
		}
		if issue := validatePredicateSpec(verifySpec, "verify"); issue != "" {
			parseIssues = append(parseIssues, Issue{File: file, Line: verifyLine, Code: IssueBadVerify, Detail: issue})
		} else {
			book.Verify[verifyName] = *verifySpec
		}
		verifyName = ""
		verifyLine = 0
		verifySpec = nil
		verifyKeys = map[string]bool{}
		section = sectionNone
	}

	for i := bodyStart; i < len(lines); i++ {
		lineNo := i + 1
		line := lines[i]
		token, rest, hasToken := firstToken(line)
		if hasToken {
			switch token {
			case tokenStep:
				finishVerify()
				finish()
				id := strings.TrimSpace(rest)
				if id == "" {
					parseIssues = append(parseIssues, Issue{File: file, Line: lineNo, Code: IssueStrayContent, Detail: tokenStep})
					continue
				}
				if _, ok := book.Steps[id]; ok {
					parseIssues = append(parseIssues, Issue{File: file, Line: lineNo, Code: IssueDuplicateStep, Detail: id})
				}
				current = &stepBuilder{
					step:       Step{ID: id},
					line:       lineNo,
					branchSeen: map[string]int{},
				}
				section = sectionNone
				continue
			case tokenSetGoal:
				finishVerify()
				if current != nil || goal != nil || strings.TrimSpace(rest) != "" {
					parseIssues = append(parseIssues, Issue{File: file, Line: lineNo, Code: IssueBadGoal, Detail: tokenSetGoal})
					continue
				}
				goal = &ResultSpec{}
				section = sectionGoal
				continue
			case tokenVerify:
				finish()
				finishVerify()
				name := strings.TrimSpace(rest)
				if !validIdentifier(name) {
					parseIssues = append(parseIssues, Issue{File: file, Line: lineNo, Code: IssueBadVerify, Detail: "invalid verify name"})
					section = sectionNone
					continue
				}
				if _, exists := book.Verify[name]; exists {
					parseIssues = append(parseIssues, Issue{File: file, Line: lineNo, Code: IssueBadVerify, Detail: "duplicate verify " + name})
					section = sectionNone
					continue
				}
				verifyName = name
				verifyLine = lineNo
				verifySpec = &ResultSpec{}
				verifyKeys = map[string]bool{}
				section = sectionVerify
				continue
			case tokenSubmit:
				// [Submit] <verify-name>:单行指令(名字在同行),无后续内容节。
				// 必须在步骤内;名字须是合法标识;每步最多一条。指向的 [Verify]
				// 是否存在留到 validate() 末尾统一校验(与分支目标一样,允许前向引用)。
				name := strings.TrimSpace(rest)
				if current == nil {
					parseIssues = append(parseIssues, Issue{File: file, Line: lineNo, Code: IssueStrayContent, Detail: tokenSubmit})
					continue
				}
				if !validIdentifier(name) {
					parseIssues = append(parseIssues, Issue{File: file, Line: lineNo, Code: IssueBadSubmit, Detail: "invalid submit name"})
					continue
				}
				if current.step.Submit != "" {
					parseIssues = append(parseIssues, Issue{File: file, Line: lineNo, Code: IssueBadSubmit, Detail: "duplicate [Submit] in step " + current.step.ID})
					continue
				}
				current.step.Submit = name
				section = sectionNone
				continue
			case tokenStepJob, tokenCheckList, tokenBranch, tokenGotcha, tokenCheckpoint:
				if current == nil || strings.TrimSpace(rest) != "" {
					parseIssues = append(parseIssues, Issue{File: file, Line: lineNo, Code: IssueStrayContent, Detail: token})
					continue
				}
				switch token {
				case tokenStepJob:
					current.seenJob = true
					section = sectionJob
				case tokenCheckList:
					current.seenList = true
					section = sectionList
				case tokenBranch:
					current.seenBranch = true
					section = sectionJump
				case tokenGotcha:
					section = sectionGotcha // 可选节,不参与 missing_section 校验
				case tokenCheckpoint:
					current.seenCheckpoint = true
					section = sectionCheckpoint
				}
				continue
			}
		}

		if section == sectionGoal {
			trimmed := strings.TrimSpace(line)
			if trimmed == "" || strings.HasPrefix(trimmed, "#") { // 整行注释:首个非空白字符为 #
				continue
			}
			if issue := parsePredicateLine(goal, goalKeys, trimmed, section); issue != "" {
				parseIssues = append(parseIssues, Issue{File: file, Line: lineNo, Code: IssueBadGoal, Detail: issue})
			}
			continue
		}
		if section == sectionVerify {
			trimmed := strings.TrimSpace(line)
			if trimmed == "" || strings.HasPrefix(trimmed, "#") { // 整行注释:首个非空白字符为 #
				continue
			}
			if issue := parsePredicateLine(verifySpec, verifyKeys, trimmed, section); issue != "" {
				parseIssues = append(parseIssues, Issue{File: file, Line: lineNo, Code: IssueBadVerify, Detail: issue})
			}
			continue
		}
		if current == nil {
			if strings.TrimSpace(line) != "" {
				parseIssues = append(parseIssues, Issue{File: file, Line: lineNo, Code: IssueStrayContent})
			}
			continue
		}
		if strings.TrimSpace(line) == "" && section != sectionJob {
			continue
		}
		switch section {
		case sectionJob:
			current.jobLines = append(current.jobLines, line)
		case sectionCheckpoint:
			current.checkpointLines = append(current.checkpointLines, line)
		case sectionList, sectionGotcha:
			if token != tokenListItem {
				parseIssues = append(parseIssues, Issue{File: file, Line: lineNo, Code: IssueStrayContent})
				continue
			}
			item := strings.TrimSpace(rest)
			if item == "" {
				parseIssues = append(parseIssues, Issue{File: file, Line: lineNo, Code: IssueStrayContent})
				continue
			}
			if section == sectionList {
				current.step.Checklist = append(current.step.Checklist, item)
			} else {
				current.step.Gotchas = append(current.step.Gotchas, item)
			}
		case sectionJump:
			key, value, ok := strings.Cut(strings.TrimSpace(line), ":")
			if !ok {
				parseIssues = append(parseIssues, Issue{File: file, Line: lineNo, Code: IssueStrayContent})
				continue
			}
			key = strings.TrimSpace(key)
			value = strings.TrimSpace(value)
			if key != "success" && key != "failure" {
				parseIssues = append(parseIssues, Issue{File: file, Line: lineNo, Code: IssueBadBranch, Detail: key})
				continue
			}
			if _, exists := current.branchSeen[key]; exists {
				parseIssues = append(parseIssues, Issue{File: file, Line: lineNo, Code: IssueBadBranch, Detail: key})
				continue
			}
			current.branchSeen[key] = lineNo
			if key == "success" {
				current.step.Branch.Success = value
			} else {
				current.step.Branch.Failure = value
			}
		default:
			if strings.TrimSpace(line) != "" {
				parseIssues = append(parseIssues, Issue{File: file, Line: lineNo, Code: IssueStrayContent})
			}
		}
	}
	finishVerify()
	finish()

	if book.VerifyPolicy == "named" && len(book.Verify) == 0 {
		parseIssues = append(parseIssues, Issue{File: file, Line: 1, Code: IssueBadFrontmatter, Detail: "verify_policy: named requires at least one [Verify] section"})
	}
	if goal != nil && goal.Verify != "" {
		// goal 别名在全部节解析完成后才解析,使 [SetGoal] 与 [Verify] 的先后顺序无关。
		if named, ok := book.Verify[goal.Verify]; ok {
			resolved := named.Clone()
			resolved.Verify = ""
			resolved.AllowOverrides = nil
			goal = &resolved
		} else {
			parseIssues = append(parseIssues, Issue{File: file, Code: IssueBadGoal, Detail: "unknown verify " + goal.Verify})
			goal = nil
		}
	}
	if goal != nil {
		if issue := validatePredicateSpec(goal, "goal"); issue != "" {
			parseIssues = append(parseIssues, Issue{File: file, Code: IssueBadGoal, Detail: issue})
		}
		book.Goal = goal
	}
	parseIssues = append(parseIssues, validate(file, book)...)
	if len(parseIssues) > 0 {
		return book, parseIssues
	}
	return book, nil
}

// parsePredicateLine 解析 [SetGoal]/[Verify] 节内的一行(key: value,封闭键集)。
// section 区分上下文:`verify:` 引用仅 [SetGoal] 合法(goal 别名),
// `allow_overrides:` 仅 [Verify] 合法(curated spec 的开口声明)。
func parsePredicateLine(spec *ResultSpec, seen map[string]bool, line, section string) string {
	key, value, ok := strings.Cut(line, ":")
	if !ok {
		return "not a key: value line"
	}
	key = strings.TrimSpace(key)
	value = strings.TrimSpace(value)
	if seen[key] {
		return "duplicate key " + key
	}
	seen[key] = true
	// verify 引用与一切其他键互斥:引用即整体采用具名谓词,不接受拼装。
	if key != "verify" && spec.Verify != "" {
		return "verify cannot be combined with other keys"
	}
	switch key {
	case "verify":
		if section == sectionVerify {
			return "verify cannot reference verify"
		}
		if len(seen) > 1 {
			return "verify cannot be combined with other keys"
		}
		if !validIdentifier(value) {
			return "invalid verify reference"
		}
		spec.Verify = value
	case "allow_overrides":
		if section != sectionVerify {
			return "allow_overrides is only allowed in [Verify] sections"
		}
		var fields []string
		if err := json.Unmarshal([]byte(value), &fields); err != nil {
			return "allow_overrides is not a JSON array" + commentHint(value)
		}
		seenField := map[string]bool{}
		for _, field := range fields {
			if field != "tests" && field != "options" {
				return `allow_overrides entries must be "tests" or "options"`
			}
			if seenField[field] {
				return "duplicate allow_overrides entry " + field
			}
			seenField[field] = true
		}
		spec.AllowOverrides = fields
	case "shell":
		if spec.Kind != "" {
			return "multiple predicate kinds"
		}
		if value == "" {
			return "empty shell command"
		}
		spec.Kind = "shell"
		spec.Command = value
	case "mcp":
		if spec.Kind != "" {
			return "multiple predicate kinds"
		}
		parts := strings.Fields(value)
		if len(parts) != 2 {
			return "mcp expects: <server> <tool>" + commentHint(value)
		}
		spec.Kind = "mcp"
		spec.Server = parts[0]
		spec.Tool = parts[1]
	case "run":
		if spec.Kind != "" {
			return "multiple predicate kinds"
		}
		if value != "" && !validRecipeID(value) {
			return "run recipe id must match [A-Za-z0-9_-][A-Za-z0-9._-]* without '..'" + commentHint(value)
		}
		spec.Kind = "run"
		spec.Recipe = value
	case "fact":
		if spec.Kind != "" {
			return "multiple predicate kinds"
		}
		if value == "" {
			return "empty fact query"
		}
		// 以 # 开头的检索词不可能命中任何符号/路径,缺席式 expect 还会借此空通过;
		// 词中混入 # 的病态路径仍然合法 —— 按语法拒绝,不做启发式猜测。
		for _, term := range strings.Fields(value) {
			if strings.HasPrefix(term, "#") {
				return "fact query term '" + term + "' cannot match any symbol" + commentHint(value)
			}
		}
		spec.Kind = "fact"
		spec.Query = value
	case "arguments":
		var args map[string]any
		if err := json.Unmarshal([]byte(value), &args); err != nil {
			return "arguments is not a JSON object" + commentHint(value)
		}
		spec.Arguments = args
	case "tests":
		var tests []string
		if err := json.Unmarshal([]byte(value), &tests); err != nil {
			return "tests is not a JSON array" + commentHint(value)
		}
		// 与运行期校验对齐(internal/verify/typed.go validateTyped):tests[] 不允许空串。
		for _, test := range tests {
			if test == "" {
				return "tests entries must not be empty"
			}
		}
		spec.Tests = tests
	case "options":
		var options map[string]any
		if err := json.Unmarshal([]byte(value), &options); err != nil {
			return "options is not a JSON object" + commentHint(value)
		}
		spec.Options = options
	case "expect":
		if !json.Valid([]byte(value)) {
			return "expect is not JSON" + commentHint(value)
		}
		spec.Expect = json.RawMessage(value)
	case "timeout_s":
		n, err := strconv.Atoi(value)
		if err != nil || n < 1 || n > MaxTimeoutS {
			return "timeout_s out of range" + commentHint(value)
		}
		spec.TimeoutS = n
	case "output_lines":
		n, err := strconv.Atoi(value)
		if err != nil || n < 1 || n > MaxOutputLines {
			return "output_lines out of range" + commentHint(value)
		}
		spec.OutputLines = n
	default:
		return "unknown key " + key
	}
	return ""
}

// commentHint 在含 '#' 的非法值的报错上点名真实病因:行内注释不受支持。
// 绝不静默剥离 —— 任何启发式剥离都会改写某人的合法值(见提案 Part 2)。
func commentHint(value string) string {
	if strings.Contains(value, "#") {
		return "; inline '#' comments are not supported (use full-line comments)"
	}
	return ""
}

// recipeIDPattern 镜像引擎的 target-id 规则(engine/arbiter_engine/runs/recipes.py
// SAFE_TARGET_ID):recipe id 会拼进文件系统路径,必须 path-safe。
var recipeIDPattern = regexp.MustCompile(`^[A-Za-z0-9_-][A-Za-z0-9._-]*$`)

func validRecipeID(value string) bool {
	return recipeIDPattern.MatchString(value) && !strings.Contains(value, "..")
}

func validatePredicateSpec(spec *ResultSpec, name string) string {
	if spec.Kind == "" {
		return "missing predicate kind"
	}
	if spec.Arguments != nil && spec.Kind != "mcp" {
		return "arguments without mcp"
	}
	if len(spec.Tests) != 0 && spec.Kind != "run" {
		return "tests without run"
	}
	if spec.Options != nil && spec.Kind != "run" {
		return "options without run"
	}
	// allow_overrides 只对 run 字段(tests/options)有意义;挂在其他 kind 上是陷阱
	// (任何按它提交的覆盖都会在运行期 Validate 被拒),解析期即封死。
	if len(spec.AllowOverrides) != 0 && spec.Kind != "run" {
		return "allow_overrides without run"
	}
	if len(spec.Expect) != 0 && spec.Kind == "shell" {
		return "expect without mcp/run/fact"
	}
	// 注:以下与运行期 verify 校验对齐的检查是就地复刻 —— playbook 不能 import
	// verify(verify 已 import playbook),各处注释指向 typed.go 中的对应实现。
	switch spec.Kind {
	case "shell":
		if spec.Command == "" {
			return "empty shell command"
		}
	case "mcp":
		if spec.Server == "" || spec.Tool == "" {
			return "incomplete mcp"
		}
		// 对应 internal/verify/typed.go ParseMCPExpect:mcp expect 必须是 JSON 数组。
		// 仅校验外形(数组);子句数量上限与逐句操作符校验是有意留在执行期
		// (internal/verify/typed.go ParseMCPExpect),解析期只 fail 结构性错误。
		if len(spec.Expect) != 0 {
			var clauses []json.RawMessage
			if err := json.Unmarshal(spec.Expect, &clauses); err != nil {
				return "mcp expect must be an array"
			}
		}
	case "run":
		// 空 recipe 的 run 谓词会流入引擎的 stub 分支并产出空洞的 checkmate;
		// 引擎(async_runs._validate_spec)与运行期(typed.go validateTyped)同样拒绝。
		if spec.Recipe == "" {
			return "run without recipe"
		}
		if len(spec.Tests) == 0 {
			return "run without tests"
		}
		if len(spec.Expect) == 0 {
			return "run without expect"
		}
		// 对应 internal/verify/typed.go ParseRunExpect:expect 必须是含 ≥1 子句的对象。
		var clauses map[string]json.RawMessage
		if err := json.Unmarshal(spec.Expect, &clauses); err != nil {
			return "run expect is not a JSON object"
		}
		if len(clauses) == 0 {
			return "run expect must contain at least one clause"
		}
	case "fact":
		if spec.Query == "" {
			return "empty fact query"
		}
		if len(spec.Expect) == 0 {
			return "fact without expect"
		}
	default:
		return "unknown predicate kind " + name
	}
	return ""
}

func ScanDir(dir string) Catalog {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return Catalog{Invalid: []Issue{{Code: IssueBadFrontmatter, Detail: err.Error()}}}
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })

	var cat Catalog
	byName := map[string][]int{}
	for _, entry := range entries {
		if entry.IsDir() || filepath.Ext(entry.Name()) != ".md" {
			continue
		}
		// FORMAT.md is the deployed grammar reference, not a playbook; the deploy
		// opening enumeration already skips it by name. Excluding it here keeps it
		// out of the catalog's invalid[] list, where ReadPlayBook would otherwise
		// surface it to the model as a bogus "playbook with bad frontmatter".
		if entry.Name() == "FORMAT.md" {
			continue
		}
		path := filepath.Join(dir, entry.Name())
		book, issues := ParseFile(path)
		if len(issues) > 0 {
			cat.Invalid = append(cat.Invalid, issues...)
			if book.Name != "" {
				cat.Entries = append(cat.Entries, CatalogEntry{File: entry.Name(), Book: book, Problems: issues})
				byName[book.Name] = append(byName[book.Name], len(cat.Entries)-1)
			}
			continue
		}
		cat.Entries = append(cat.Entries, CatalogEntry{File: entry.Name(), Book: book})
		byName[book.Name] = append(byName[book.Name], len(cat.Entries)-1)
	}

	for name, indexes := range byName {
		if len(indexes) < 2 {
			continue
		}
		for _, idx := range indexes {
			file := cat.Entries[idx].File
			cat.Entries[idx].Problems = append(cat.Entries[idx].Problems, Issue{File: file, Code: IssueNameConflict, Detail: name})
			cat.Invalid = append(cat.Invalid, Issue{File: file, Code: IssueNameConflict, Detail: name})
		}
	}
	return cat
}

func (c Catalog) LoadableNames() []string {
	names := []string{}
	for _, entry := range c.Entries {
		if len(entry.Problems) == 0 {
			names = append(names, entry.Book.Name)
		}
	}
	sort.Strings(names)
	return names
}

func (c Catalog) Find(name string) (CatalogEntry, string) {
	var found []CatalogEntry
	for _, entry := range c.Entries {
		if entry.Book.Name == name {
			found = append(found, entry)
		}
	}
	if len(found) == 0 {
		return CatalogEntry{}, CodePlaybookNotFound
	}
	if len(found) > 1 {
		return found[0], CodeNameConflict
	}
	for _, issue := range found[0].Problems {
		if issue.Code == IssueNameConflict {
			return found[0], CodeNameConflict
		}
	}
	if len(found[0].Problems) > 0 {
		return found[0], CodePlaybookInvalid
	}
	return found[0], ""
}

func validate(file string, book Playbook) []Issue {
	var issues []Issue
	if book.Name == "" || book.Description == "" {
		issues = append(issues, Issue{File: file, Code: IssueBadFrontmatter})
	}
	if book.MaxSteps < 0 || book.MaxSteps > MaxStepsCeiling {
		issues = append(issues, Issue{File: file, Code: IssueBadMaxSteps, Detail: strconv.Itoa(book.MaxSteps)})
	}
	if len(book.Steps) == 0 {
		issues = append(issues, Issue{File: file, Code: IssueNoSteps})
		return issues
	}
	for _, id := range book.order {
		step := book.Steps[id]
		if strings.TrimSpace(step.Job) == "" {
			issues = append(issues, Issue{File: file, Code: IssueEmptyJob, Detail: id})
		}
		// 关卡步骤无清单(由 [Checkpoint] 裁决);任务步骤必须有非空清单。
		if step.Checkpoint == "" && len(step.Checklist) == 0 {
			issues = append(issues, Issue{File: file, Code: IssueEmptyChecklist, Detail: id})
		}
		// 关卡步骤没有可执行谓词,绑定 [Submit] 无意义。
		if step.Checkpoint != "" && step.Submit != "" {
			issues = append(issues, Issue{File: file, Code: IssueBadCheckpoint, Detail: "checkpoint step " + id + " cannot also bind [Submit]"})
		}
		if step.Branch.Success == "" || step.Branch.Failure == "" {
			issues = append(issues, Issue{File: file, Code: IssueBadBranch, Detail: id})
		}
		for _, target := range []string{step.Branch.Success, step.Branch.Failure} {
			if target == "" || target == EndTarget {
				continue
			}
			if _, ok := book.Steps[target]; !ok {
				issues = append(issues, Issue{File: file, Code: IssueUnknownBranchTarget, Detail: target})
			}
		}
		if step.Submit != "" {
			if _, ok := book.Verify[step.Submit]; !ok {
				issues = append(issues, Issue{File: file, Code: IssueBadSubmit, Detail: "[Submit] " + step.Submit + " names no [Verify] predicate"})
			}
		}
	}
	return issues
}

func normalizeCapabilities(values []string) []string {
	out := make([]string, 0, len(values))
	for _, value := range values {
		out = append(out, strings.TrimSpace(value))
	}
	return out
}

func validateCapabilities(file string, values []string) []Issue {
	var issues []Issue
	seen := map[string]bool{}
	for _, value := range values {
		if !validIdentifier(value) {
			issues = append(issues, Issue{File: file, Code: IssueBadVerify, Detail: "invalid capability " + value})
			continue
		}
		if value != "recipes" {
			issues = append(issues, Issue{File: file, Code: IssueBadVerify, Detail: "unknown capability " + value})
			continue
		}
		if seen[value] {
			issues = append(issues, Issue{File: file, Code: IssueBadVerify, Detail: "duplicate capability " + value})
			continue
		}
		seen[value] = true
	}
	return issues
}

// identifierPattern mirrors FORMAT.md:122 ([Verify]/[Submit] names, verify:
// refs, capabilities are ASCII [A-Za-z0-9_-]+) — like recipeIDPattern above,
// the grammar is ASCII-only, so unicode.IsLetter/IsDigit would over-accept
// confusable/homoglyph names the documented grammar forbids.
var identifierPattern = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)

func validIdentifier(value string) bool {
	return identifierPattern.MatchString(value)
}

func parseHeader(file string, lines []string) (frontmatter, int, []Issue) {
	if len(lines) == 0 || strings.TrimSpace(lines[0]) != "---" {
		return frontmatter{}, 0, []Issue{{File: file, Line: 1, Code: IssueBadFrontmatter}}
	}
	end := -1
	for i := 1; i < len(lines); i++ {
		if strings.TrimSpace(lines[i]) == "---" {
			end = i
			break
		}
	}
	if end < 0 {
		return frontmatter{}, 0, []Issue{{File: file, Line: 1, Code: IssueBadFrontmatter}}
	}
	var meta frontmatter
	header := strings.Join(lines[1:end], "\n")
	if err := yaml.Unmarshal([]byte(header), &meta); err != nil {
		return frontmatter{}, 0, []Issue{{File: file, Line: 1, Code: IssueBadFrontmatter, Detail: err.Error()}}
	}
	if strings.TrimSpace(meta.Name) == "" || strings.TrimSpace(meta.Description) == "" {
		return frontmatter{}, 0, []Issue{{File: file, Line: 1, Code: IssueBadFrontmatter}}
	}
	return meta, end + 1, nil
}

func splitLines(data []byte) []string {
	data = bytes.ReplaceAll(data, []byte("\r\n"), []byte("\n"))
	data = bytes.TrimSuffix(data, []byte("\n"))
	if len(data) == 0 {
		return nil
	}
	parts := strings.Split(string(data), "\n")
	return parts
}

func firstToken(line string) (string, string, bool) {
	trimmed := strings.TrimLeftFunc(line, unicode.IsSpace)
	if trimmed == "" {
		return "", "", false
	}
	index := strings.IndexFunc(trimmed, unicode.IsSpace)
	if index < 0 {
		return trimmed, "", true
	}
	return trimmed[:index], trimmed[index+1:], true
}
