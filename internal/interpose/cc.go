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
	src         string
	out         string
	compile     bool
}

func Run(root string, args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) < 2 || args[0] != "--" {
		fmt.Fprintln(stderr, "usage: arbiter cc -- <real-compiler> [args...]")
		return 2
	}
	inv := classify(collapseSelfWrap(args[1:]))
	if len(inv.argv) == 0 {
		fmt.Fprintln(stderr, "usage: arbiter cc -- <real-compiler> [args...]")
		return 2
	}
	if inv.compile {
		if err := executableUnavailable(inv.argv[0]); err != nil {
			_ = appendJournal(root, journalEntry{
				Argv:   inv.journalArgv,
				CWD:    root,
				Src:    inv.src,
				Out:    inv.out,
				TS:     time.Now().UTC().Format(time.RFC3339Nano),
				Miss:   true,
				Reason: err.Error(),
			})
			fmt.Fprintln(stderr, err)
			return 127
		}
	}
	if inv.compile {
		_ = appendJournal(root, journalEntry{
			Argv: inv.journalArgv,
			CWD:  root,
			Src:  inv.src,
			Out:  inv.out,
			TS:   time.Now().UTC().Format(time.RFC3339Nano),
		})
	}
	cmd := exec.Command(inv.argv[0], inv.argv[1:]...)
	cmd.Dir = root
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
			_ = appendJournal(root, journalEntry{
				Argv:   inv.journalArgv,
				CWD:    root,
				Src:    inv.src,
				Out:    inv.out,
				TS:     time.Now().UTC().Format(time.RFC3339Nano),
				Miss:   true,
				Reason: err.Error(),
			})
		}
		if errors.Is(err, exec.ErrNotFound) || os.IsNotExist(err) {
			return 127
		}
		fmt.Fprintln(stderr, err)
		return 1
	}
	return 0
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
	if len(argv) < 3 || argv[1] != "cc" || argv[2] != "--" {
		return argv
	}
	exe, err := os.Executable()
	if err != nil {
		return argv
	}
	if samePath(exe, argv[0]) {
		return argv[3:]
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

func classify(argv []string) invocation {
	expanded := expandArgs(argv)
	inv := invocation{argv: argv, journalArgv: expanded}
	hasCompile := false
	for i := 1; i < len(expanded); i++ {
		arg := expanded[i]
		switch {
		case arg == "-c":
			hasCompile = true
		case arg == "-o" && i+1 < len(expanded):
			inv.out = expanded[i+1]
			i++
		case strings.HasPrefix(arg, "-o") && len(arg) > 2:
			inv.out = arg[2:]
		case isSource(arg):
			if inv.src == "" {
				inv.src = arg
			}
		}
	}
	inv.compile = hasCompile && inv.src != ""
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
