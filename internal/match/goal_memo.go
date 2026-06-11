package match

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"sort"

	"github.com/HoldThatThgt/arbiter/internal/playbook"

	"gopkg.in/yaml.v3"
)

// goalMemoCap 限制 GoalMemo 的条目数:超出时按 StoredAt 淘汰最旧的。
const goalMemoCap = 32

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
	census, ok := s.goalCensusDigest()
	if !ok {
		// 普查不可靠(不可读文件、坏目录等):空摘要表示"本次裁决跳过 memo",
		// 调用方对空摘要既不查也不记,goal 谓词照常真实执行。
		return "", nil
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

// goalCensusDigest 计算工作区内容普查摘要。ok=false 表示普查不可靠
// (出现不可读的常规文件、不可达目录等),调用方应跳过本次 memo;
// memo 只是优化,任何普查障碍都绝不能让整次裁决(CheckStepJob)失败。
func (s *Store) goalCensusDigest() (string, bool) {
	var entries []string
	usable := true
	_ = filepath.WalkDir(s.Root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			usable = false
			return filepath.SkipAll
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
		rel, relErr := filepath.Rel(s.Root, path)
		if relErr != nil {
			usable = false
			return filepath.SkipAll
		}
		switch {
		case d.Type()&fs.ModeSymlink != 0:
			// 符号链接以链接目标字符串参与普查:坏链接也能 Readlink,不会读穿;
			// 重定向链接会改变摘要,从而正确地作废 memo。
			target, linkErr := os.Readlink(path)
			if linkErr != nil {
				usable = false
				return filepath.SkipAll
			}
			entries = append(entries, filepath.ToSlash(rel)+"\x00link\x00"+sha256String([]byte(target)))
		case !d.Type().IsRegular():
			// FIFO/socket/设备文件:读取可能阻塞或失败,直接跳过不入册。
			return nil
		default:
			data, readErr := os.ReadFile(path)
			if readErr != nil {
				// 常规文件不可读(EACCES、与删除竞争的 ENOENT 等):
				// 禁用本次 memo,而不是让裁决整体报错。
				usable = false
				return filepath.SkipAll
			}
			entries = append(entries, filepath.ToSlash(rel)+"\x00"+sha256String(data))
		}
		return nil
	})
	if !usable {
		return "", false
	}
	sort.Strings(entries)
	digest := sha256.New()
	for _, entry := range entries {
		digest.Write([]byte(entry))
		digest.Write([]byte{0})
	}
	return hex.EncodeToString(digest.Sum(nil)), true
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
		Report:   stored,
		StoredAt: utcNow(),
	}
	// 防无界增长:只保留最近 goalMemoCap 条。StoredAt 是 UTC RFC3339,
	// 字典序即时间序;刚写入的条目绝不被淘汰。
	for len(m.GoalMemo) > goalMemoCap {
		oldest := ""
		for key, entry := range m.GoalMemo {
			if key == digest {
				continue
			}
			if oldest == "" || entry.StoredAt < m.GoalMemo[oldest].StoredAt {
				oldest = key
			}
		}
		delete(m.GoalMemo, oldest)
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
