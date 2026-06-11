package playbook

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"unicode"

	"gopkg.in/yaml.v3"
)

const (
	tokenStep      = "[STEP]"
	tokenStepJob   = "[StepJob]"
	tokenCheckList = "[CheckList]"
	tokenBranch    = "[Branch]"
	tokenSetGoal   = "[SetGoal]"
	tokenVerify    = "[Verify]"
	tokenGotcha    = "[Gotcha]"
	tokenListItem  = "-"

	sectionNone   = ""
	sectionJob    = "job"
	sectionList   = "checklist"
	sectionJump   = "branch"
	sectionGoal   = "goal"
	sectionVerify = "verify"
	sectionGotcha = "gotcha"
)

type frontmatter struct {
	Name         string   `yaml:"name"`
	Description  string   `yaml:"description"`
	MaxSteps     int      `yaml:"max_steps"`
	Capabilities []string `yaml:"capabilities"`
}

type stepBuilder struct {
	step       Step
	line       int
	seenJob    bool
	seenList   bool
	seenBranch bool
	jobLines   []string
	branchSeen map[string]int
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
		Verify:       map[string]ResultSpec{},
		Steps:        map[string]Step{},
	}
	var current *stepBuilder
	section := sectionNone
	parseIssues := validateCapabilities(file, book.Capabilities)
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
		if !current.seenList {
			parseIssues = append(parseIssues, Issue{File: file, Line: current.line, Code: IssueMissingSection, Detail: tokenCheckList})
		}
		if !current.seenBranch {
			parseIssues = append(parseIssues, Issue{File: file, Line: current.line, Code: IssueMissingSection, Detail: tokenBranch})
		}
		current.step.Job = strings.Join(current.jobLines, "\n")
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
			case tokenStepJob, tokenCheckList, tokenBranch, tokenGotcha:
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
				}
				continue
			}
		}

		if section == sectionGoal {
			if strings.TrimSpace(line) == "" {
				continue
			}
			if issue := parsePredicateLine(goal, goalKeys, strings.TrimSpace(line)); issue != "" {
				parseIssues = append(parseIssues, Issue{File: file, Line: lineNo, Code: IssueBadGoal, Detail: issue})
			}
			continue
		}
		if section == sectionVerify {
			if strings.TrimSpace(line) == "" {
				continue
			}
			if issue := parsePredicateLine(verifySpec, verifyKeys, strings.TrimSpace(line)); issue != "" {
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
func parsePredicateLine(spec *ResultSpec, seen map[string]bool, line string) string {
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
	switch key {
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
			return "mcp expects: <server> <tool>"
		}
		spec.Kind = "mcp"
		spec.Server = parts[0]
		spec.Tool = parts[1]
	case "run":
		if spec.Kind != "" {
			return "multiple predicate kinds"
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
		spec.Kind = "fact"
		spec.Query = value
	case "arguments":
		var args map[string]any
		if err := json.Unmarshal([]byte(value), &args); err != nil {
			return "arguments is not a JSON object"
		}
		spec.Arguments = args
	case "tests":
		var tests []string
		if err := json.Unmarshal([]byte(value), &tests); err != nil {
			return "tests is not a JSON array"
		}
		spec.Tests = tests
	case "options":
		var options map[string]any
		if err := json.Unmarshal([]byte(value), &options); err != nil {
			return "options is not a JSON object"
		}
		spec.Options = options
	case "expect":
		if !json.Valid([]byte(value)) {
			return "expect is not JSON"
		}
		spec.Expect = json.RawMessage(value)
	case "timeout_s":
		n, err := strconv.Atoi(value)
		if err != nil || n < 1 || n > MaxTimeoutS {
			return "timeout_s out of range"
		}
		spec.TimeoutS = n
	case "output_lines":
		n, err := strconv.Atoi(value)
		if err != nil || n < 1 || n > MaxOutputLines {
			return "output_lines out of range"
		}
		spec.OutputLines = n
	default:
		return "unknown key " + key
	}
	return ""
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
	if len(spec.Expect) != 0 && spec.Kind == "shell" {
		return "expect without mcp/run/fact"
	}
	switch spec.Kind {
	case "shell":
		if spec.Command == "" {
			return "empty shell command"
		}
	case "mcp":
		if spec.Server == "" || spec.Tool == "" {
			return "incomplete mcp"
		}
	case "run":
		if len(spec.Tests) == 0 {
			return "run without tests"
		}
		if len(spec.Expect) == 0 {
			return "run without expect"
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
		if len(step.Checklist) == 0 {
			issues = append(issues, Issue{File: file, Code: IssueEmptyChecklist, Detail: id})
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

func validIdentifier(value string) bool {
	if value == "" {
		return false
	}
	for _, r := range value {
		if unicode.IsLetter(r) || unicode.IsDigit(r) || r == '_' || r == '-' {
			continue
		}
		return false
	}
	return true
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
