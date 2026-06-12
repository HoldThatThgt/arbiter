package match

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
)

// FrozenTestPaths 读对局状态,返回当前冻结的测试文件(仓根相对、排序)。
// 供 guard 预防层使用:只读、best-effort,任何读/解析问题或非 active 对局
// 返回 nil(fail-open,门控故障绝不阻塞会话)。
func FrozenTestPaths(root string) []string {
	data, err := os.ReadFile(filepath.Join(root, ".arbiter", "match", "run", "state.json"))
	if err != nil {
		return nil
	}
	var m struct {
		Status      string            `json:"status"`
		FrozenTests map[string]string `json:"frozen_tests"`
	}
	if json.Unmarshal(data, &m) != nil || m.Status != StatusActive {
		return nil
	}
	paths := make([]string, 0, len(m.FrozenTests))
	for rel := range m.FrozenTests {
		paths = append(paths, rel)
	}
	sort.Strings(paths)
	return paths
}

// RegisterTestOutput 回报本次登记后处于冻结状态的全部测试文件(仓根相对、
// 排序),以及它们的内容哈希前缀,便于 test-author 在 report 里引用。
type RegisterTestOutput struct {
	Frozen []FrozenTest `json:"frozen"`
}

type FrozenTest struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

// RegisterTest 冻结一批测试文件:记录 {仓根相对路径 → 内容 sha256}。
// 冻结是 append-only 且不可改写——已冻结路径再次登记若内容不同即拒。
// 登记后,任何谓词裁决前都会重算这些文件的哈希(见 frozenViolation),
// 不符即判负,从而"测试一经注册无人可改"。test-author 写完测试、跑过标准
// 后调用它;此后 implementer 只能改产品代码让冻结测试由 run 转 pass。
func (s *Store) RegisterTest(paths []string) (RegisterTestOutput, error) {
	if len(paths) == 0 {
		return RegisterTestOutput{}, &ToolError{Code: playbook.CodeTestRegister, Message: "no test paths — pass the test file(s) you wrote and proved"}
	}
	type entry struct{ rel, hash string }
	entries := make([]entry, 0, len(paths))
	for _, p := range paths {
		rel, hash, err := s.hashUnderRoot(p)
		if err != nil {
			return RegisterTestOutput{}, err
		}
		entries = append(entries, entry{rel, hash})
	}

	out, err := s.withLock(func(m *Match) (*Match, any, error) {
		if m == nil || m.Status != StatusActive {
			return nil, nil, &ToolError{Code: playbook.CodeNoActiveMatch, Message: "no active match — RegisterTest only works inside a loaded match"}
		}
		if m.FrozenTests == nil {
			m.FrozenTests = map[string]string{}
		}
		for _, e := range entries {
			if existing, ok := m.FrozenTests[e.rel]; ok && existing != e.hash {
				return nil, nil, &ToolError{Code: playbook.CodeTestRegister, Message: "test " + e.rel + " is already frozen and cannot be re-registered with different content — a registered test is immutable for the rest of the match"}
			}
			m.FrozenTests[e.rel] = e.hash
		}
		registered := make([]string, len(entries))
		for i, e := range entries {
			registered[i] = e.rel
		}
		s.append("test_registered", map[string]any{"match_id": m.ID, "paths": registered})
		return m, RegisterTestOutput{Frozen: frozenList(m.FrozenTests)}, nil
	})
	if err != nil {
		return RegisterTestOutput{}, err
	}
	return out.(RegisterTestOutput), nil
}

// hashUnderRoot 把一个测试路径归一为仓根相对路径并计算内容哈希;路径越出
// 仓根或文件不可读一律报错(冻结的对象必须是仓内真实文件)。
func (s *Store) hashUnderRoot(p string) (rel, hash string, err error) {
	if strings.TrimSpace(p) == "" {
		return "", "", &ToolError{Code: playbook.CodeTestRegister, Message: "empty test path"}
	}
	abs := p
	if !filepath.IsAbs(abs) {
		abs = filepath.Join(s.Root, p)
	}
	abs = filepath.Clean(abs)
	rel, e := filepath.Rel(s.Root, abs)
	if e != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
		return "", "", &ToolError{Code: playbook.CodeTestRegister, Message: "test path is outside the repo: " + p}
	}
	// 拒绝叶子符号链接:词法 Rel 判定挡不住仓内软链指向仓外文件——那会冻结一份
	// 实体在仓外、可绕开 guard 与哈希检测随意改动的"测试"。冻结对象必须是
	// 仓内真实文件。
	if info, lerr := os.Lstat(abs); lerr == nil && info.Mode()&os.ModeSymlink != 0 {
		return "", "", &ToolError{Code: playbook.CodeTestRegister, Message: "test path is a symlink, not a real in-repo file: " + p + " — freeze the actual test file"}
	}
	// 父目录也可能是软链(叶子 Lstat 不跟随父级)。解析整条路径后确认真实位置仍
	// 在仓内,堵住经软链父目录把冻结对象指向仓外。root 自身也可能含软链(macOS
	// /var、/tmp),一并解析后比对,避免误伤仓内真实文件。
	if realAbs, lerr := filepath.EvalSymlinks(abs); lerr == nil {
		realRoot := s.Root
		if rr, rerr := filepath.EvalSymlinks(s.Root); rerr == nil {
			realRoot = rr
		}
		if rrel, re := filepath.Rel(realRoot, realAbs); re != nil || rrel == ".." || strings.HasPrefix(rrel, ".."+string(os.PathSeparator)) {
			return "", "", &ToolError{Code: playbook.CodeTestRegister, Message: "test path resolves outside the repo via a symlink: " + p}
		}
	}
	data, e := os.ReadFile(abs)
	if e != nil {
		return "", "", &ToolError{Code: playbook.CodeTestRegister, Message: "cannot read test file " + p + ": " + e.Error()}
	}
	return filepath.ToSlash(rel), sha256Hex(data), nil
}

// frozenViolation 重算每个冻结测试的内容哈希,返回首个被改写/删除文件的
// 相对路径(无违例返回空串)。这就是"by any means"的检测:无论经由哪种
// 途径改动,内容一变即被发现,tampered 测试永远拿不到 pass。
func frozenViolation(root string, frozen map[string]string) string {
	paths := make([]string, 0, len(frozen))
	for rel := range frozen {
		paths = append(paths, rel)
	}
	sort.Strings(paths) // 违例报告稳定
	for _, rel := range paths {
		data, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(rel)))
		if err != nil || sha256Hex(data) != frozen[rel] {
			return rel
		}
	}
	return ""
}

// copyStringMap 在锁内取冻结表快照,使锁外的磁盘核对不依赖对局状态。
func copyStringMap(in map[string]string) map[string]string {
	if len(in) == 0 {
		return nil
	}
	out := make(map[string]string, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

func frozenList(frozen map[string]string) []FrozenTest {
	out := make([]FrozenTest, 0, len(frozen))
	for rel, hash := range frozen {
		out = append(out, FrozenTest{Path: rel, SHA256: shortHash(hash)})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Path < out[j].Path })
	return out
}

// shortHash 取内容哈希前 12 位作展示。sha256Hex 恒为 64 位十六进制,这里只是
// 显示截断;对异常短值(正常路径不会出现)原样返回以防越界。
func shortHash(h string) string {
	if len(h) > 12 {
		return h[:12]
	}
	return h
}
