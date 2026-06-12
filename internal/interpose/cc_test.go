package interpose

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// Expected values below were verified against Python shlex.split, which is
// what the engine uses to expand response files
// (engine/arbiter_engine/shared/compile_db.py _expand_response_files).
func TestShlexSplitMatchesPythonShlex(t *testing.T) {
	cases := []struct {
		in   string
		want []string
	}{
		{`-c "src/space dir/hello.c" -o out.o`, []string{"-c", "src/space dir/hello.c", "-o", "out.o"}},
		{`-c 'single quoted path.c'`, []string{"-c", "single quoted path.c"}},
		{"-c\nsrc/a.c\n-o\tbuild/a.o\r\n", []string{"-c", "src/a.c", "-o", "build/a.o"}},
		{`a\b`, []string{"ab"}},
		{`back\ slash`, []string{"back slash"}},
		{`"esc \" quote"`, []string{`esc " quote`}},
		{`"keep \n backslash"`, []string{`keep \n backslash`}},
		{`"double \\ backslash"`, []string{`double \ backslash`}},
		{`'no \ escape'`, []string{`no \ escape`}},
		{`''`, []string{""}},
		{`mixed"abc"'def'`, []string{"mixedabcdef"}},
		{"a\\\nb", []string{"a\nb"}},
		{" \t\r\n", nil},
		{"", nil},
	}
	for _, tc := range cases {
		got, err := shlexSplit(tc.in)
		if err != nil {
			t.Errorf("shlexSplit(%q) error: %v", tc.in, err)
			continue
		}
		if !reflect.DeepEqual(got, tc.want) {
			t.Errorf("shlexSplit(%q) = %#v, want %#v", tc.in, got, tc.want)
		}
	}
}

func TestShlexSplitRejectsMalformedInput(t *testing.T) {
	for _, in := range []string{`"unterminated`, `'unterminated`, `trailing\`} {
		if got, err := shlexSplit(in); err == nil {
			t.Errorf("shlexSplit(%q) = %#v, want error", in, got)
		}
	}
}

func TestDiscoverRoot(t *testing.T) {
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, ".arbiter"), 0o755); err != nil {
		t.Fatal(err)
	}
	deep := filepath.Join(root, "build", "x", "y")
	if err := os.MkdirAll(deep, 0o755); err != nil {
		t.Fatal(err)
	}
	if got := DiscoverRoot(deep); got != root {
		t.Fatalf("DiscoverRoot(%q) = %q, want %q", deep, got, root)
	}
	if got := DiscoverRoot(root); got != root {
		t.Fatalf("DiscoverRoot(root) = %q, want %q", got, root)
	}
	orphan := t.TempDir()
	if got := DiscoverRoot(orphan); got != orphan {
		t.Fatalf("DiscoverRoot fallback = %q, want cwd %q", got, orphan)
	}
	filed := t.TempDir()
	if err := os.WriteFile(filepath.Join(filed, ".arbiter"), nil, 0o644); err != nil {
		t.Fatal(err)
	}
	if got := DiscoverRoot(filed); got != filed {
		t.Fatalf("DiscoverRoot with .arbiter file = %q, want cwd fallback %q", got, filed)
	}
}

func TestExpandArgsKeepsRawArgWhenResponseFileMalformed(t *testing.T) {
	rsp := filepath.Join(t.TempDir(), "bad.rsp")
	if err := os.WriteFile(rsp, []byte(`"unterminated`), 0o644); err != nil {
		t.Fatal(err)
	}
	got := expandArgs([]string{"cc", "@" + rsp})
	want := []string{"cc", "@" + rsp}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("expandArgs = %#v, want %#v", got, want)
	}
}
