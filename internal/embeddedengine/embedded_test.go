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

	cmd := exec.Command(python, "-m", "compileall", "-q", filepath.Join(repo, RootRel, "arbiter_engine"))
	cmd.Env = append(os.Environ(), "PYTHONDONTWRITEBYTECODE=") // ensure bytecode is written
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("compileall: %v\n%s", err, out)
	}
	if _, err := os.Stat(filepath.Join(repo, RootRel, "arbiter_engine", "__pycache__")); err != nil {
		t.Fatalf("compileall produced no __pycache__: %v", err)
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
