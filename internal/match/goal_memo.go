package match

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"sort"

	"github.com/HoldThatThgt/arbiter/internal/playbook"

	"gopkg.in/yaml.v3"
)

func (s *Store) goalMemoEnabled() bool {
	data, err := os.ReadFile(filepath.Join(s.Root, ".arbiter", "config.yml"))
	if err != nil {
		return false
	}
	var cfg struct {
		Match struct {
			GoalMemo bool `yaml:"goal_memo"`
		} `yaml:"match"`
	}
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return false
	}
	return cfg.Match.GoalMemo
}

func (s *Store) goalMemoDigest(m *Match, spec playbook.ResultSpec) (string, error) {
	census, err := s.goalCensusDigest()
	if err != nil {
		return "", err
	}
	specJSON, err := json.Marshal(spec)
	if err != nil {
		return "", err
	}
	pinJSON, err := json.Marshal(m.RecipesPin)
	if err != nil {
		return "", err
	}
	payload := map[string]string{
		"census":    census,
		"goal_spec": sha256String(specJSON),
		"recipes":   sha256String(pinJSON),
		"toolchain": toolchainHash(),
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}
	return sha256String(raw), nil
}

func (s *Store) goalCensusDigest() (string, error) {
	var entries []string
	err := filepath.WalkDir(s.Root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if path != s.Root {
				switch d.Name() {
				case ".arbiter", ".git":
					return filepath.SkipDir
				}
			}
			return nil
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(s.Root, path)
		if err != nil {
			return err
		}
		entries = append(entries, filepath.ToSlash(rel)+"\x00"+sha256String(data))
		return nil
	})
	if err != nil {
		return "", err
	}
	sort.Strings(entries)
	digest := sha256.New()
	for _, entry := range entries {
		digest.Write([]byte(entry))
		digest.Write([]byte{0})
	}
	return hex.EncodeToString(digest.Sum(nil)), nil
}

func memoizedGoalReport(entry GoalMemoEntry) *GoalReport {
	report := entry.Report
	report.Memoized = true
	return &report
}

func rememberGoalMemo(m *Match, digest string, report *GoalReport) {
	if digest == "" || report == nil || report.Verdict != TaskPass {
		return
	}
	if m.GoalMemo == nil {
		m.GoalMemo = map[string]GoalMemoEntry{}
	}
	stored := *report
	stored.Memoized = false
	m.GoalMemo[digest] = GoalMemoEntry{
		Digest:   digest,
		Report:   stored,
		StoredAt: utcNow(),
	}
}

func toolchainHash() string {
	exe, _ := os.Executable()
	info, err := os.Stat(exe)
	stamp := ""
	if err == nil {
		stamp = fmt.Sprintf("%d:%d", info.Size(), info.ModTime().UnixNano())
	}
	return sha256String([]byte(runtime.Version() + "\x00" + exe + "\x00" + stamp))
}

func sha256String(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}
