package embeddedengine

import (
	"os"
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

func tamper(t *testing.T, repo string) {
	t.Helper()
	path := filepath.Join(repo, RootRel, "arbiter_engine", "__init__.py")
	if err := os.WriteFile(path, []byte("tampered = True\n"), 0o644); err != nil {
		t.Fatal(err)
	}
}
