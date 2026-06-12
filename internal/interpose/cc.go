package interpose

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

type journalEntry struct {
	Argv   []string `json:"argv"`
	CWD    string   `json:"cwd"`
	Src    string   `json:"src,omitempty"`
	Out    string   `json:"out,omitempty"`
	TS     string   `json:"ts"`
	Miss   bool     `json:"miss,omitempty"`
	Reason string   `json:"reason,omitempty"`
}

type invocation struct {
	argv        []string
	journalArgv []string
	srcs        []string
	out         string
	compile     bool
}

// Run executes one interposed compiler invocation: journal it (when it
// compiles sources), then exec the real compiler with exit code passed
// through bit-exact. root is the deployment root that owns the journal; cwd
// is the directory the build invoked the shim from — entries record it so the
// engine resolves relative paths exactly as the compiler did (ADR-0014: the
// two were conflated before, sending subdir/out-of-tree journals into stray
// <cwd>/.arbiter/ trees the engine never reads).
func Run(root, cwd string, args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) < 2 || args[0] != "--" {
		fmt.Fprintln(stderr, "usage: arbiter cc [--root DIR] -- <real-compiler> [args...]")
		return 2
	}
	inv := classify(collapseSelfWrap(args[1:]))
	if len(inv.argv) == 0 {
		fmt.Fprintln(stderr, "usage: arbiter cc [--root DIR] -- <real-compiler> [args...]")
		return 2
	}
	if inv.compile {
		if err := executableUnavailable(inv.argv[0]); err != nil {
			journalAll(root, cwd, inv, true, err.Error())
			fmt.Fprintln(stderr, err)
			return 127
		}
		journalAll(root, cwd, inv, false, "")
	}
	cmd := exec.Command(inv.argv[0], inv.argv[1:]...)
	cmd.Dir = cwd
	cmd.Env = os.Environ()
	cmd.Stdin = stdin
	cmd.Stdout = stdout
	cmd.Stderr = stderr
	if err := cmd.Run(); err != nil {
		var exit *exec.ExitError
		if errors.As(err, &exit) {
			return exit.ExitCode()
		}
		if inv.compile {
			journalAll(root, cwd, inv, true, err.Error())
		}
		if errors.Is(err, exec.ErrNotFound) || os.IsNotExist(err) {
			return 127
		}
		fmt.Fprintln(stderr, err)
		return 1
	}
	return 0
}

// journalAll appends one entry per source file: the engine consumes
// single-src records (compile_db.py), so a driver line compiling several TUs
// journals as several records sharing argv and timestamp.
func journalAll(root, cwd string, inv invocation, miss bool, reason string) {
	out := inv.out
	if len(inv.srcs) > 1 {
		out = "" // -o names the link product, not any single TU's object
	}
	ts := time.Now().UTC().Format(time.RFC3339Nano)
	for _, src := range inv.srcs {
		_ = appendJournal(root, journalEntry{
			Argv:   inv.journalArgv,
			CWD:    cwd,
			Src:    src,
			Out:    out,
			TS:     ts,
			Miss:   miss,
			Reason: reason,
		})
	}
}

// DiscoverRoot walks from cwd toward the filesystem root and returns the
// first directory containing an .arbiter directory — the deployment root.
// No match falls back to cwd itself, which is exactly where pre-discovery
// generations journaled (fail-open: never break a build over layout).
func DiscoverRoot(cwd string) string {
	dir := filepath.Clean(cwd)
	for {
		if info, err := os.Stat(filepath.Join(dir, ".arbiter")); err == nil && info.IsDir() {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return filepath.Clean(cwd)
		}
		dir = parent
	}
}

func executableUnavailable(path string) error {
	if strings.ContainsRune(path, os.PathSeparator) {
		info, err := os.Stat(path)
		if err != nil {
			return err
		}
		if info.IsDir() {
			return fmt.Errorf("%s is a directory", path)
		}
		return nil
	}
	_, err := exec.LookPath(path)
	return err
}

func collapseSelfWrap(argv []string) []string {
	if len(argv) < 3 || argv[1] != "cc" {
		return argv
	}
	rest := argv[2:]
	if len(rest) >= 3 && rest[0] == "--root" {
		rest = rest[2:]
	}
	if rest[0] != "--" || len(rest) < 2 {
		return argv
	}
	exe, err := os.Executable()
	if err != nil {
		return argv
	}
	if samePath(exe, argv[0]) {
		return rest[1:]
	}
	return argv
}

func samePath(a, b string) bool {
	aa, errA := filepath.EvalSymlinks(a)
	bb, errB := filepath.EvalSymlinks(b)
	if errA == nil && errB == nil {
		return aa == bb
	}
	absA, errA := filepath.Abs(a)
	absB, errB := filepath.Abs(b)
	return errA == nil && errB == nil && absA == absB
}

// classify decides whether an invocation compiles sources. Any recognized
// source argument means compilation — with or without -c (a driver line like
// `cc src/a.c -o app` compiles and links in one step; requiring -c made such
// builds journal nothing, silently) — unless a mode flag replaces compilation
// with preprocessing, assembly, dependency scanning, or a bare syntax check.
func classify(argv []string) invocation {
	expanded := expandArgs(argv)
	inv := invocation{argv: argv, journalArgv: expanded}
	noCompile := false
	for i := 1; i < len(expanded); i++ {
		arg := expanded[i]
		switch {
		case arg == "-E" || arg == "-S" || arg == "-M" || arg == "-MM" || arg == "-fsyntax-only":
			noCompile = true
		case arg == "-o" && i+1 < len(expanded):
			inv.out = expanded[i+1]
			i++
		case strings.HasPrefix(arg, "-o") && len(arg) > 2:
			inv.out = arg[2:]
		case isSource(arg):
			inv.srcs = append(inv.srcs, arg)
		}
	}
	inv.compile = len(inv.srcs) > 0 && !noCompile
	return inv
}

func expandArgs(argv []string) []string {
	out := make([]string, 0, len(argv))
	for _, arg := range argv {
		if strings.HasPrefix(arg, "@") && len(arg) > 1 {
			expanded, err := readResponseFile(arg[1:])
			if err == nil {
				out = append(out, expanded...)
				continue
			}
		}
		out = append(out, arg)
	}
	return out
}

func readResponseFile(path string) ([]string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return shlexSplit(string(data))
}

// shlexSplit splits response file text with POSIX shlex semantics, matching
// the Python side's shlex.split in
// engine/arbiter_engine/shared/compile_db.py _expand_response_files so quoted
// arguments containing spaces journal identically on both sides:
//   - tokens separate on space, tab, CR, and LF (shlex.whitespace);
//   - single quotes are literal with no escapes inside;
//   - inside double quotes a backslash escapes only `"` and `\` (otherwise
//     the backslash is kept);
//   - outside quotes a backslash escapes any single character;
//   - unterminated quotes and a trailing escape are errors (shlex raises
//     ValueError), which makes expandArgs fall back to the raw @arg.
func shlexSplit(text string) ([]string, error) {
	var (
		tokens  []string
		token   []rune
		have    bool
		quote   rune
		escaped bool
	)
	for _, r := range text {
		switch {
		case escaped:
			if quote == '"' && r != '"' && r != '\\' {
				token = append(token, '\\')
			}
			token = append(token, r)
			escaped = false
		case quote == '\'':
			if r == '\'' {
				quote = 0
			} else {
				token = append(token, r)
			}
		case quote == '"':
			switch r {
			case '"':
				quote = 0
			case '\\':
				escaped = true
			default:
				token = append(token, r)
			}
		case r == '\\':
			escaped = true
			have = true
		case r == '\'' || r == '"':
			quote = r
			have = true
		case r == ' ' || r == '\t' || r == '\r' || r == '\n':
			if have {
				tokens = append(tokens, string(token))
				token = token[:0]
				have = false
			}
		default:
			token = append(token, r)
			have = true
		}
	}
	if escaped {
		return nil, errors.New("no escaped character")
	}
	if quote != 0 {
		return nil, errors.New("no closing quotation")
	}
	if have {
		tokens = append(tokens, string(token))
	}
	return tokens, nil
}

func isSource(arg string) bool {
	switch strings.ToLower(filepath.Ext(arg)) {
	case ".c", ".cc", ".cpp", ".cxx", ".c++", ".m", ".mm":
		return true
	default:
		return false
	}
}

func appendJournal(root string, entry journalEntry) error {
	path := compileJournalPath(root)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	data, err := json.Marshal(entry)
	if err != nil {
		return err
	}
	data = append(data, '\n')
	_, err = f.Write(data)
	return err
}

func compileJournalPath(root string) string {
	buildID := os.Getenv("ARBITER_BUILD_ID")
	if buildID == "" {
		buildID = "default"
	}
	return filepath.Join(root, ".arbiter", "facts", "run", "compile-journal."+buildID+".jsonl")
}
