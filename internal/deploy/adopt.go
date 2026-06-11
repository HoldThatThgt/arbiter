package deploy

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

type AdoptReport struct {
	MovedPlaybooks      []string
	MigratedRecipes     bool
	MigratedFactsConfig bool
	DeletedDerived      []string
	RemovedMCPServers   []string
	Checklist           []ChecklistItem
}

type ChecklistItem struct {
	Path  string
	Line  int
	Token string
	Text  string
}

func (r AdoptReport) String() string {
	var b strings.Builder
	b.WriteString("arbiter adopt complete\n")
	if len(r.Checklist) > 0 {
		b.WriteString("manual rewrite checklist:\n")
		for _, item := range r.Checklist {
			fmt.Fprintf(&b, "- %s:%d %s %s\n", item.Path, item.Line, item.Token, item.Text)
		}
	}
	return b.String()
}

func Adopt(root string) (AdoptReport, error) {
	var report AdoptReport
	var err error
	if report.MovedPlaybooks, err = adoptPlaybooks(root); err != nil {
		return report, err
	}
	if report.MigratedRecipes, err = adoptRecipes(root); err != nil {
		return report, err
	}
	if report.MigratedFactsConfig, err = adoptCipherConfig(root); err != nil {
		return report, err
	}
	if report.DeletedDerived, err = deleteDerivedState(root); err != nil {
		return report, err
	}
	if report.RemovedMCPServers, err = removeLegacyMCP(root); err != nil {
		return report, err
	}
	if report.Checklist, err = scanLegacyTokens(root); err != nil {
		return report, err
	}
	return report, nil
}

func adoptPlaybooks(root string) ([]string, error) {
	src := filepath.Join(root, ".chess", "playbook")
	if _, err := os.Stat(src); os.IsNotExist(err) {
		return nil, nil
	}
	var moved []string
	err := filepath.WalkDir(src, func(path string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		dst := filepath.Join(root, dirPlaybook, rel)
		if err := moveFile(path, dst); err != nil {
			return err
		}
		moved = append(moved, filepath.ToSlash(rel))
		return nil
	})
	if err != nil {
		return nil, err
	}
	_ = os.RemoveAll(src)
	sort.Strings(moved)
	return moved, nil
}

func adoptRecipes(root string) (bool, error) {
	src, ok := firstExisting(root, []string{
		".crun-mcp/recipes.yaml",
		".crun-mcp/recipes.yml",
		".crun/recipes.yaml",
		"crun-mcp.yaml",
	})
	if !ok {
		return false, nil
	}
	data, err := os.ReadFile(filepath.Join(root, src))
	if err != nil {
		return false, err
	}
	out := []byte("# Migrated from " + filepath.ToSlash(src) + ".\n")
	out = append(out, data...)
	if err := atomicWrite(filepath.Join(root, fileRecipes), out, 0o644); err != nil {
		return false, err
	}
	_ = os.Remove(filepath.Join(root, src))
	return true, nil
}

func adoptCipherConfig(root string) (bool, error) {
	src := filepath.Join(root, ".cipher", "config.yml")
	data, err := os.ReadFile(src)
	if os.IsNotExist(err) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	config := renderFactsConfig(string(data))
	if err := atomicWrite(filepath.Join(root, fileConfig), []byte(config), 0o644); err != nil {
		return false, err
	}
	_ = os.Remove(src)
	return true, nil
}

func renderFactsConfig(legacy string) string {
	pool := findSectionInt(legacy, "extractor", "worker_count")
	incremental, hasIncremental := findSectionBool(legacy, "incremental", "enabled")
	var b strings.Builder
	b.WriteString("# Migrated from .cipher/config.yml.\nfacts:\n  extractor: \"cipher-2\"\n")
	if hasIncremental {
		fmt.Fprintf(&b, "  incremental: %t\n", incremental)
	}
	if pool > 0 {
		fmt.Fprintf(&b, "  index_on_build:\n    pool: %d\n", pool)
	}
	b.WriteString("# Legacy cipher config preserved for manual review:\n")
	for _, line := range strings.Split(strings.TrimSuffix(legacy, "\n"), "\n") {
		b.WriteString("# ")
		b.WriteString(line)
		b.WriteByte('\n')
	}
	return b.String()
}

func deleteDerivedState(root string) ([]string, error) {
	var deleted []string
	for _, rel := range []string{
		".chess/run", ".chess/log",
		".cipher/run", ".cipher/snapshots", ".cipher/log",
		".crun-mcp/run", ".crun-mcp/log", ".crun-mcp/cache",
	} {
		path := filepath.Join(root, rel)
		if _, err := os.Stat(path); os.IsNotExist(err) {
			continue
		}
		if err := os.RemoveAll(path); err != nil {
			return nil, err
		}
		deleted = append(deleted, rel)
	}
	return deleted, nil
}

func removeLegacyMCP(root string) ([]string, error) {
	path := filepath.Join(root, fileMCP)
	if _, err := os.Stat(path); os.IsNotExist(err) {
		return nil, nil
	}
	cfg, err := readJSON(path)
	if err != nil {
		return nil, err
	}
	servers, _ := cfg["mcpServers"].(map[string]any)
	if servers == nil {
		return nil, nil
	}
	var removed []string
	for _, name := range []string{"chess", "chess-player", "chess-curator", "chess-executor", "cipher-2", "crun-mcp"} {
		if _, ok := servers[name]; ok {
			delete(servers, name)
			removed = append(removed, name)
		}
	}
	if len(removed) == 0 {
		return nil, nil
	}
	sort.Strings(removed)
	return removed, writeJSON(path, cfg, 0o644)
}

func scanLegacyTokens(root string) ([]ChecklistItem, error) {
	tokens := []string{"LoadPlayBook", "crun-mcp", "cipher-2", "mcp__chess", "mcp__crun", "mcp__cipher"}
	var items []ChecklistItem
	err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if shouldSkipScanDir(root, path, d.Name()) {
				return filepath.SkipDir
			}
			return nil
		}
		data, err := os.ReadFile(path)
		if err != nil || bytes.IndexByte(data, 0) >= 0 {
			return err
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		for lineNo, line := range strings.Split(string(data), "\n") {
			for _, token := range tokens {
				if containsWholeToken(line, token) {
					items = append(items, ChecklistItem{Path: filepath.ToSlash(rel), Line: lineNo + 1, Token: token, Text: strings.TrimSpace(line)})
				}
			}
		}
		return nil
	})
	sort.Slice(items, func(i, j int) bool {
		if items[i].Path != items[j].Path {
			return items[i].Path < items[j].Path
		}
		if items[i].Line != items[j].Line {
			return items[i].Line < items[j].Line
		}
		return items[i].Token < items[j].Token
	})
	return items, err
}

func moveFile(src, dst string) error {
	data, err := os.ReadFile(src)
	if err != nil {
		return err
	}
	if existing, err := os.ReadFile(dst); err == nil {
		if !bytes.Equal(existing, data) {
			return &Error{Kind: "adopt_conflict", Message: "adopt target differs: " + dst}
		}
		return os.Remove(src)
	}
	if err := atomicWrite(dst, data, 0o644); err != nil {
		return err
	}
	return os.Remove(src)
}

func firstExisting(root string, rels []string) (string, bool) {
	for _, rel := range rels {
		if _, err := os.Stat(filepath.Join(root, rel)); err == nil {
			return rel, true
		}
	}
	return "", false
}

func findSectionInt(text, section, key string) int {
	value, ok := findSectionValue(text, section, key)
	if !ok {
		return 0
	}
	n, _ := strconv.Atoi(value)
	return n
}

func findSectionBool(text, section, key string) (bool, bool) {
	value, ok := findSectionValue(text, section, key)
	if !ok {
		return false, false
	}
	switch strings.ToLower(value) {
	case "true":
		return true, true
	case "false":
		return false, true
	default:
		return false, false
	}
}

func findSectionValue(text, section, key string) (string, bool) {
	current := ""
	for _, raw := range strings.Split(text, "\n") {
		trimmed := strings.TrimSpace(strings.Split(raw, "#")[0])
		if trimmed == "" {
			continue
		}
		if !strings.HasPrefix(raw, " ") && strings.HasSuffix(trimmed, ":") {
			current = strings.TrimSuffix(trimmed, ":")
			continue
		}
		if current == section && strings.HasPrefix(strings.TrimSpace(raw), key+":") {
			parts := strings.SplitN(strings.TrimSpace(raw), ":", 2)
			return strings.Trim(strings.TrimSpace(parts[1]), `"'`), true
		}
	}
	return "", false
}

func shouldSkipScanDir(root, path, name string) bool {
	if path == root {
		return false
	}
	switch name {
	case ".git", "vendor", "import", ".arbiter":
		return true
	default:
		return false
	}
}

func containsWholeToken(line, token string) bool {
	for start := 0; ; {
		idx := strings.Index(line[start:], token)
		if idx < 0 {
			return false
		}
		idx += start
		beforeOK := idx == 0 || !tokenChar(line[idx-1])
		after := idx + len(token)
		afterOK := after == len(line) || !tokenChar(line[after])
		if beforeOK && afterOK {
			return true
		}
		start = idx + len(token)
	}
}

func tokenChar(c byte) bool {
	return c == '_' || c == '-' || ('0' <= c && c <= '9') || ('A' <= c && c <= 'Z') || ('a' <= c && c <= 'z')
}
