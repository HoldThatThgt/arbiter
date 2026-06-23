package embeddedengine

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestVerifyDetectsTamperBeforeFirstSuccess(t *testing.T) {
	repo := t.TempDir()
	manifest, err := Unpack(repo)
	if err != nil {
		t.Fatal(err)
	}
	if manifest.Files == 0 || manifest.Digest == "" {
		t.Fatalf("manifest = %#v", manifest)
	}
	tamper(t, repo)
	if _, err := Verify(repo, manifest.Digest); err == nil || !strings.Contains(err.Error(), "digest mismatch") {
		t.Fatalf("err = %v, want digest mismatch", err)
	}
}

func TestVerifyMemoizesSuccessfulVerification(t *testing.T) {
	repo := t.TempDir()
	manifest, err := Unpack(repo)
	if err != nil {
		t.Fatal(err)
	}
	first, err := Verify(repo, manifest.Digest)
	if err != nil {
		t.Fatal(err)
	}

	// Documented trade-off: once a (root, digest) pair verifies, later
	// Verify calls in the same process return the cached success without
	// re-hashing, even if the tree changed afterwards.
	tamper(t, repo)
	cached, err := Verify(repo, manifest.Digest)
	if err != nil {
		t.Fatalf("memoized verify: %v", err)
	}
	if cached != first {
		t.Fatalf("cached manifest = %#v, want %#v", cached, first)
	}

	// A different expected digest is a different key and must re-hash.
	if _, err := Verify(repo, "0000000000000000000000000000000000000000000000000000000000000000"); err == nil || !strings.Contains(err.Error(), "digest mismatch") {
		t.Fatalf("err = %v, want digest mismatch", err)
	}

	// A different root is a different key and must re-hash too.
	other := t.TempDir()
	otherManifest, err := Unpack(other)
	if err != nil {
		t.Fatal(err)
	}
	tamper(t, other)
	if _, err := Verify(other, otherManifest.Digest); err == nil || !strings.Contains(err.Error(), "digest mismatch") {
		t.Fatalf("err = %v, want digest mismatch", err)
	}
}

func TestDigestIgnoresPythonBytecode(t *testing.T) {
	repo := t.TempDir()
	manifest, err := Unpack(repo)
	if err != nil {
		t.Fatal(err)
	}

	pycache := filepath.Join(repo, RootRel, "arbiter_engine", "__pycache__")
	if err := os.MkdirAll(pycache, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(pycache, "x.cpython-311.pyc"), []byte("bytecode"), 0o644); err != nil {
		t.Fatal(err)
	}
	// Stray bytecode outside __pycache__ must be ignored too.
	if err := os.WriteFile(filepath.Join(repo, RootRel, "arbiter_engine", "stray.pyo"), []byte("bytecode"), 0o644); err != nil {
		t.Fatal(err)
	}

	recomputed, err := Digest(repo)
	if err != nil {
		t.Fatal(err)
	}
	if recomputed.Digest != manifest.Digest {
		t.Fatalf("digest changed after writing bytecode: %s -> %s", manifest.Digest, recomputed.Digest)
	}
	if _, err := Verify(repo, manifest.Digest); err != nil {
		t.Fatalf("verify after writing bytecode: %v", err)
	}
}

// TestDigestIgnoresOrphanedTempFile is the regression test for a crashed
// writeFile (CreateTemp then rename) leaving a stray ".tmp-*" in the tree:
// hashing it would diverge the digest from the unpack-time manifest and
// trip a spurious "digest mismatch". Digest must skip it like bytecode.
func TestDigestIgnoresOrphanedTempFile(t *testing.T) {
	repo := t.TempDir()
	manifest, err := Unpack(repo)
	if err != nil {
		t.Fatal(err)
	}

	// Mirror the prefix writeFile passes to os.CreateTemp(dir, ".tmp-*").
	orphan := filepath.Join(repo, RootRel, "arbiter_engine", ".tmp-1234567890")
	if err := os.WriteFile(orphan, []byte("partial unpack"), 0o644); err != nil {
		t.Fatal(err)
	}

	recomputed, err := Digest(repo)
	if err != nil {
		t.Fatal(err)
	}
	if recomputed.Digest != manifest.Digest {
		t.Fatalf("digest changed after orphaned temp file: %s -> %s", manifest.Digest, recomputed.Digest)
	}
	if _, err := Verify(repo, manifest.Digest); err != nil {
		t.Fatalf("verify after orphaned temp file: %v", err)
	}
}

// TestVerifySurvivesRealBytecodeGeneration is the regression test for the
// live failure where importing the freshly-unpacked engine (e.g. init's
// version probe) wrote __pycache__/*.pyc and made every later digest check
// fail. It runs a real interpreter against the unpacked tree.
func TestVerifySurvivesRealBytecodeGeneration(t *testing.T) {
	python, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("python3 not available")
	}
	repo := t.TempDir()
	manifest, err := Unpack(repo)
	if err != nil {
		t.Fatal(err)
	}

	// -b 强制 legacy 同目录 .pyc:Apple 系统 Python 设有 sys.pycache_prefix
	// (集中缓存目录),普通 compileall 不会在树内产生 __pycache__,
	// 该断言在 mac 上永远失败;-b 绕过前缀,树内必有字节码。
	tree := filepath.Join(repo, RootRel, "arbiter_engine")
	cmd := exec.Command(python, "-m", "compileall", "-q", "-b", tree)
	cmd.Env = append(os.Environ(), "PYTHONDONTWRITEBYTECODE=") // ensure bytecode is written
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("compileall: %v\n%s", err, out)
	}
	pycs := 0
	if err := filepath.WalkDir(tree, func(path string, d os.DirEntry, err error) error {
		if err == nil && !d.IsDir() && filepath.Ext(path) == ".pyc" {
			pycs++
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if pycs == 0 {
		t.Fatal("compileall produced no in-tree bytecode")
	}

	if _, err := Verify(repo, manifest.Digest); err != nil {
		t.Fatalf("verify after real bytecode generation: %v", err)
	}
}

func tamper(t *testing.T, repo string) {
	t.Helper()
	path := filepath.Join(repo, RootRel, "arbiter_engine", "__init__.py")
	if err := os.WriteFile(path, []byte("tampered = True\n"), 0o644); err != nil {
		t.Fatal(err)
	}
}
