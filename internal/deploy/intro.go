package deploy

import (
	"bytes"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

type MacroScanReport struct {
	Checklist         []ChecklistItem
	SuggestedKeyFlags []string
}

func ScanInstrumentationMacros(root string) (MacroScanReport, error) {
	flags := map[string]struct{}{}
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
			for _, hit := range instrumentationHits(line) {
				items = append(items, ChecklistItem{
					Path:  filepath.ToSlash(rel),
					Line:  lineNo + 1,
					Token: hit.token,
					Text:  strings.TrimSpace(line),
				})
				if hit.flag != "" {
					flags[hit.flag] = struct{}{}
				}
			}
		}
		return nil
	})
	if err != nil {
		return MacroScanReport{}, err
	}
	sort.Slice(items, func(i, j int) bool {
		if items[i].Path != items[j].Path {
			return items[i].Path < items[j].Path
		}
		if items[i].Line != items[j].Line {
			return items[i].Line < items[j].Line
		}
		return items[i].Token < items[j].Token
	})
	suggested := make([]string, 0, len(flags))
	for flag := range flags {
		suggested = append(suggested, flag)
	}
	sort.Strings(suggested)
	return MacroScanReport{Checklist: items, SuggestedKeyFlags: suggested}, nil
}

type instrumentationHit struct {
	token string
	flag  string
}

func instrumentationHits(line string) []instrumentationHit {
	var hits []instrumentationHit
	if containsWholeToken(line, "__SANITIZE_ADDRESS__") {
		hits = append(hits, instrumentationHit{"__SANITIZE_ADDRESS__", "-fsanitize=address"})
	}
	if containsWholeToken(line, "__SANITIZE_THREAD__") {
		hits = append(hits, instrumentationHit{"__SANITIZE_THREAD__", "-fsanitize=thread"})
	}
	if containsWholeToken(line, "__has_feature") {
		switch {
		case containsWholeToken(line, "address_sanitizer"):
			hits = append(hits, instrumentationHit{"__has_feature(address_sanitizer)", "-fsanitize=address"})
		case containsWholeToken(line, "thread_sanitizer"):
			hits = append(hits, instrumentationHit{"__has_feature(thread_sanitizer)", "-fsanitize=thread"})
		case containsWholeToken(line, "memory_sanitizer"):
			hits = append(hits, instrumentationHit{"__has_feature(memory_sanitizer)", "-fsanitize=memory"})
		case strings.Contains(line, "_sanitizer"):
			hits = append(hits, instrumentationHit{"__has_feature(*_sanitizer)", ""})
		}
	}
	return hits
}
