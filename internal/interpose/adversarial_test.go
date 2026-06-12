package interpose

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
)

var (
	arbiterOnce sync.Once
	arbiterBin  string
	arbiterErr  error
)

func TestInterposeAdversarialMatrix(t *testing.T) {
	bin := requireArbiterCC(t)

	t.Run("response files", func(t *testing.T) {
		work := t.TempDir()
		fake, log := fakeCompiler(t, work, 0)
		src := writeFile(t, work, "src/hello.c", "int hello;\n")
		rsp := writeFile(t, work, "args.rsp", fmt.Sprintf("-c\n%s\n-o\n%s\n", src, filepath.Join(work, "out", "hello.o")))

		runCC(t, bin, work, "rsp", fake, "@"+rsp)

		entries := readJournal(t, work, "rsp")
		if got := entries[0]["src"]; got != src {
			t.Fatalf("src = %#v want %q", got, src)
		}
		if !strings.Contains(readText(t, log), "@"+rsp) {
			t.Fatalf("compiler did not receive original response arg")
		}
	})

	t.Run("quoted response file arguments", func(t *testing.T) {
		// Mirrors the engine's shlex.split expansion in
		// engine/arbiter_engine/shared/compile_db.py (_expand_response_files,
		// exercised by engine/tests/test_compile_db.py): quoted paths with
		// spaces must journal as single tokens.
		work := t.TempDir()
		fake, log := fakeCompiler(t, work, 0)
		src := writeFile(t, work, "src/space dir/hello.c", "int hello;\n")
		out := filepath.Join(work, "out dir", "hello.o")
		rsp := writeFile(t, work, "quoted.rsp", fmt.Sprintf("-c \"%s\"\n-o '%s'\n", src, out))

		runCC(t, bin, work, "quoted", fake, "@"+rsp)

		entries := readJournal(t, work, "quoted")
		if got := entries[0]["src"]; got != src {
			t.Fatalf("src = %#v want %q", got, src)
		}
		if got := entries[0]["out"]; got != out {
			t.Fatalf("out = %#v want %q", got, out)
		}
		argv := stringSlice(t, entries[0]["argv"])
		want := []string{fake, "-c", src, "-o", out}
		if strings.Join(argv, "\x00") != strings.Join(want, "\x00") {
			t.Fatalf("journal argv = %#v want %#v", argv, want)
		}
		if !strings.Contains(readText(t, log), "@"+rsp) {
			t.Fatalf("compiler did not receive original response arg")
		}
	})

	t.Run("stacked ccache", func(t *testing.T) {
		work := t.TempDir()
		fake, log := fakeCompiler(t, work, 0)
		ccache := writeScript(t, filepath.Join(work, "ccache"), "#!/bin/sh\nexec \"$@\"\n")
		src := writeFile(t, work, "src/a.c", "int a;\n")
		out := filepath.Join(work, "out", "a.o")

		runCC(t, bin, work, "ccache", ccache, fake, "-c", src, "-o", out)

		entries := readJournal(t, work, "ccache")
		argv := stringSlice(t, entries[0]["argv"])
		if argv[0] != ccache || argv[1] != fake {
			t.Fatalf("argv = %#v", argv[:2])
		}
		if !strings.Contains(readText(t, log), "arg:"+src) {
			t.Fatalf("stacked compiler did not run: %s", readText(t, log))
		}
	})

	t.Run("multi arch and depfile flags", func(t *testing.T) {
		work := t.TempDir()
		fake, _ := fakeCompiler(t, work, 0)
		src := writeFile(t, work, "src/multi.c", "int multi;\n")
		out := filepath.Join(work, "out", "multi.o")
		dep := filepath.Join(work, "dep", "multi.d")

		runCC(t, bin, work, "multi", fake, "-arch", "arm64", "-arch", "x86_64", "-MD", "-MF", dep, "-c", src, "-o", out)

		if _, err := os.Stat(dep); err != nil {
			t.Fatalf("depfile not passed through: %v", err)
		}
		entries := readJournal(t, work, "multi")
		if entries[0]["out"] != out {
			t.Fatalf("out = %#v want %q", entries[0]["out"], out)
		}
	})

	t.Run("parallel journal integrity", func(t *testing.T) {
		work := t.TempDir()
		fake, _ := fakeCompiler(t, work, 0)
		const count = 32
		errs := make(chan error, count)
		for i := 0; i < count; i++ {
			i := i
			go func() {
				src := writeFile(t, work, fmt.Sprintf("src/p%02d.c", i), "int p;\n")
				out := filepath.Join(work, "out", fmt.Sprintf("p%02d.o", i))
				errs <- runCCErr(bin, work, "parallel", fake, "-c", src, "-o", out)
			}()
		}
		for i := 0; i < count; i++ {
			if err := <-errs; err != nil {
				t.Fatal(err)
			}
		}
		if got := len(readJournal(t, work, "parallel")); got != count {
			t.Fatalf("journal lines = %d want %d", got, count)
		}
	})

	t.Run("interrupted build leaves consumable partial journal", func(t *testing.T) {
		work := t.TempDir()
		fake, _ := fakeCompiler(t, work, 23)
		src := writeFile(t, work, "src/fail.c", "int fail;\n")

		err := runCCErr(bin, work, "interrupted", fake, "-c", src, "-o", filepath.Join(work, "out", "fail.o"))
		if exitCode(err) != 23 {
			t.Fatalf("exit = %v want 23", err)
		}
		entries := readJournal(t, work, "interrupted")
		if len(entries) != 1 || entries[0]["src"] != src {
			t.Fatalf("partial journal = %#v", entries)
		}
	})

	t.Run("compiler not found is propagated and journaled as miss", func(t *testing.T) {
		work := t.TempDir()
		missing := filepath.Join(work, "missing-cc")

		err := runCCErr(bin, work, "missing", missing, "-c", "x.c", "-o", "x.o")
		if err == nil {
			t.Fatal("missing compiler succeeded")
		}
		entries := readJournal(t, work, "missing")
		if entries[0]["miss"] != true {
			t.Fatalf("miss entry = %#v", entries[0])
		}
	})

	t.Run("hostile paths and symlinked compiler", func(t *testing.T) {
		work := t.TempDir()
		fake, _ := fakeCompiler(t, work, 0)
		link := filepath.Join(work, "cc link")
		if err := os.Symlink(fake, link); err != nil {
			t.Fatal(err)
		}
		src := writeFile(t, work, "src hostile/q'uote file.c", "int q;\n")
		out := filepath.Join(work, "out hostile", "q'uote file.o")

		runCC(t, bin, work, "hostile", link, "-c", src, "-o", out)

		entries := readJournal(t, work, "hostile")
		if entries[0]["src"] != src || entries[0]["out"] != out {
			t.Fatalf("hostile journal = %#v", entries[0])
		}
	})

	t.Run("subdir compile journals to discovered root", func(t *testing.T) {
		work := t.TempDir()
		fake, _ := fakeCompiler(t, work, 0)
		if err := os.MkdirAll(filepath.Join(work, ".arbiter"), 0o755); err != nil {
			t.Fatal(err)
		}
		sub := filepath.Join(work, "build", "CMakeFiles")
		if err := os.MkdirAll(sub, 0o755); err != nil {
			t.Fatal(err)
		}
		writeFile(t, work, "src/deep.c", "int deep;\n")

		if err := runCCFrom(bin, sub, work, "subdir", "--", fake, "-c", "../../src/deep.c", "-o", "deep.o"); err != nil {
			t.Fatal(err)
		}

		entries := readJournal(t, work, "subdir") // at the deployment root, not under build/
		wantCWD, err := filepath.EvalSymlinks(sub)
		if err != nil {
			t.Fatal(err)
		}
		gotCWD, _ := entries[0]["cwd"].(string)
		if resolved, err := filepath.EvalSymlinks(gotCWD); err != nil || resolved != wantCWD {
			t.Fatalf("cwd = %q (resolved err %v), want %q", gotCWD, err, wantCWD)
		}
		if entries[0]["src"] != "../../src/deep.c" {
			t.Fatalf("src = %#v, want the relative path the compiler saw", entries[0]["src"])
		}
		if _, err := os.Stat(filepath.Join(sub, ".arbiter")); !os.IsNotExist(err) {
			t.Fatalf("stray .arbiter in build subdir (stat err %v)", err)
		}
	})

	t.Run("explicit root override wins", func(t *testing.T) {
		work := t.TempDir()
		fake, _ := fakeCompiler(t, work, 0)
		other := t.TempDir()
		src := writeFile(t, work, "src/r.c", "int r;\n")

		if err := runCCFrom(bin, work, work, "rooted", "--root", other, "--", fake, "-c", src, "-o", filepath.Join(work, "r.o")); err != nil {
			t.Fatal(err)
		}

		entries := readJournal(t, other, "rooted")
		if len(entries) != 1 || entries[0]["src"] != src {
			t.Fatalf("journal at --root = %#v", entries)
		}
		if _, err := os.Stat(journalPath(work, "rooted")); !os.IsNotExist(err) {
			t.Fatalf("journal also written under cwd (stat err %v)", err)
		}
	})

	t.Run("one-step compile and link journals per source", func(t *testing.T) {
		work := t.TempDir()
		fake, _ := fakeCompiler(t, work, 0)
		a := writeFile(t, work, "src/a.c", "int a;\n")
		b := writeFile(t, work, "src/b.c", "int b;\n")

		runCC(t, bin, work, "onestep", fake, a, b, "-o", filepath.Join(work, "app"))

		entries := readJournal(t, work, "onestep")
		if len(entries) != 2 || entries[0]["src"] != a || entries[1]["src"] != b {
			t.Fatalf("one-step journal = %#v", entries)
		}
		if out, ok := entries[0]["out"]; ok && out != "" {
			t.Fatalf("multi-TU out = %#v, want empty (the -o is the link product)", out)
		}
	})

	t.Run("preprocess, assembly, depscan, syntax-only do not journal", func(t *testing.T) {
		work := t.TempDir()
		fake, _ := fakeCompiler(t, work, 0)
		src := writeFile(t, work, "src/e.c", "int e;\n")

		for _, flag := range []string{"-E", "-S", "-M", "-MM", "-fsyntax-only"} {
			runCC(t, bin, work, "modeflag", fake, flag, src)
		}

		if _, err := os.Stat(journalPath(work, "modeflag")); !os.IsNotExist(err) {
			t.Fatalf("mode-flag invocations journaled (stat err %v)", err)
		}
	})

	t.Run("self wrap with explicit root collapses", func(t *testing.T) {
		work := t.TempDir()
		fake, log := fakeCompiler(t, work, 0)
		src := writeFile(t, work, "src/swr.c", "int swr;\n")

		runCC(t, bin, work, "selfroot", bin, "cc", "--root", work, "--", fake, "-c", src, "-o", filepath.Join(work, "swr.o"))

		if got := strings.Count(readText(t, log), "argv0:"); got != 1 {
			t.Fatalf("fake compiler invocations = %d want 1", got)
		}
		if got := len(readJournal(t, work, "selfroot")); got != 1 {
			t.Fatalf("journal lines = %d want 1", got)
		}
	})

	t.Run("self wrap collapses", func(t *testing.T) {
		work := t.TempDir()
		fake, log := fakeCompiler(t, work, 0)
		src := writeFile(t, work, "src/self.c", "int self;\n")

		runCC(t, bin, work, "self", bin, "cc", "--", fake, "-c", src, "-o", filepath.Join(work, "out", "self.o"))

		if got := strings.Count(readText(t, log), "argv0:"); got != 1 {
			t.Fatalf("fake compiler invocations = %d want 1", got)
		}
		if got := len(readJournal(t, work, "self")); got != 1 {
			t.Fatalf("journal lines = %d want 1", got)
		}
	})
}

// requireArbiterCC fails hard when the probe fails: arbiter cc is implemented,
// so a broken probe is a regression, never a reason to skip the matrix.
func requireArbiterCC(t *testing.T) string {
	t.Helper()
	bin := buildArbiter(t)
	work := t.TempDir()
	fake, _ := fakeCompiler(t, work, 0)
	if err := runCCErr(bin, work, "probe", fake, "--probe"); err != nil {
		t.Fatalf("arbiter cc probe failed: %v", err)
	}
	return bin
}

// buildArbiter builds the non-race arbiter binary once per test process. The
// binary lives in its own MkdirTemp directory (not t.TempDir, which is torn
// down when the first calling test finishes) so every test in the package can
// share the cached path.
func buildArbiter(t *testing.T) string {
	t.Helper()
	arbiterOnce.Do(func() {
		root := repoRoot()
		dir, err := os.MkdirTemp("", "arbiter-interpose-")
		if err != nil {
			arbiterErr = err
			return
		}
		bin := filepath.Join(dir, "arbiter")
		cmd := exec.Command("go", "build", "-o", bin, "./cmd/arbiter")
		cmd.Dir = root
		out, err := cmd.CombinedOutput()
		if err != nil {
			arbiterErr = fmt.Errorf("%w: %s", err, out)
			return
		}
		arbiterBin = bin
	})
	if arbiterErr != nil {
		t.Fatal(arbiterErr)
	}
	return arbiterBin
}

func repoRoot() string {
	_, file, _, _ := runtime.Caller(0)
	return filepath.Clean(filepath.Join(filepath.Dir(file), "..", ".."))
}

func fakeCompiler(t *testing.T, work string, code int) (string, string) {
	t.Helper()
	log := filepath.Join(work, "fake-cc.log")
	script := fmt.Sprintf(`#!/bin/sh
{
  printf 'argv0:%%s\n' "$0"
  for arg in "$@"; do printf 'arg:%%s\n' "$arg"; done
  printf -- '---\n'
} >> "$FAKE_CC_LOG"
out=""
dep=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "-o" ]; then out="$arg"; fi
  if [ "$prev" = "-MF" ]; then dep="$arg"; fi
  prev="$arg"
done
if [ -n "$out" ]; then mkdir -p "$(dirname "$out")" && : > "$out"; fi
if [ -n "$dep" ]; then mkdir -p "$(dirname "$dep")" && : > "$dep"; fi
exit %d
`, code)
	return writeScript(t, filepath.Join(work, "fake cc"), script), log
}

func writeScript(t *testing.T, path, body string) string {
	t.Helper()
	if err := os.WriteFile(path, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	return path
}

func writeFile(t *testing.T, root, rel, body string) string {
	t.Helper()
	path := filepath.Join(root, rel)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func runCC(t *testing.T, bin, work, buildID string, args ...string) {
	t.Helper()
	if err := runCCErr(bin, work, buildID, args...); err != nil {
		t.Fatal(err)
	}
}

// runCCFrom runs the shim with an explicit working directory and raw
// post-"cc" arguments (so tests can exercise --root and subdir invocations).
func runCCFrom(bin, dir, work, buildID string, ccArgs ...string) error {
	cmd := exec.Command(bin, append([]string{"cc"}, ccArgs...)...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), "ARBITER_BUILD_ID="+buildID, "FAKE_CC_LOG="+filepath.Join(work, "fake-cc.log"))
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("%w: %s", err, out)
	}
	return nil
}

func runCCErr(bin, work, buildID string, args ...string) error {
	cmd := exec.Command(bin, append([]string{"cc", "--"}, args...)...)
	cmd.Dir = work
	cmd.Env = append(os.Environ(), "ARBITER_BUILD_ID="+buildID, "FAKE_CC_LOG="+filepath.Join(work, "fake-cc.log"))
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("%w: %s", err, out)
	}
	return nil
}

func journalPath(work, buildID string) string {
	return filepath.Join(work, ".arbiter", "facts", "run", "compile-journal."+buildID+".jsonl")
}

func readJournal(t *testing.T, work, buildID string) []map[string]any {
	t.Helper()
	f, err := os.Open(journalPath(work, buildID))
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	var entries []map[string]any
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		var entry map[string]any
		if err := json.Unmarshal(scanner.Bytes(), &entry); err != nil {
			t.Fatalf("bad journal line %q: %v", scanner.Text(), err)
		}
		entries = append(entries, entry)
	}
	if err := scanner.Err(); err != nil {
		t.Fatal(err)
	}
	return entries
}

func readText(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func stringSlice(t *testing.T, value any) []string {
	t.Helper()
	raw, ok := value.([]any)
	if !ok {
		t.Fatalf("not an array: %#v", value)
	}
	out := make([]string, len(raw))
	for i, v := range raw {
		s, ok := v.(string)
		if !ok {
			t.Fatalf("not a string at %d: %#v", i, v)
		}
		out[i] = s
	}
	return out
}

func exitCode(err error) int {
	if err == nil {
		return 0
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		return exit.ExitCode()
	}
	return -1
}
