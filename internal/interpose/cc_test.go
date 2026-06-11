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
