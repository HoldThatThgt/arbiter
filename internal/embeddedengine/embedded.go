package embeddedengine

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sync"

	enginebundle "github.com/HoldThatThgt/arbiter/engine"
)

const RootRel = ".arbiter/engine"

type Manifest struct {
	Root   string
	Digest string
	Files  int
}

func Unpack(repo string) (Manifest, error) {
	root := filepath.Join(repo, RootRel)
	if err := os.RemoveAll(root); err != nil {
		return Manifest{}, err
	}
	files := 0
	if err := fs.WalkDir(enginebundle.FS, "arbiter_engine", func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		data, err := enginebundle.FS.ReadFile(path)
		if err != nil {
			return err
		}
		if err := writeFile(filepath.Join(root, filepath.FromSlash(path)), data); err != nil {
			return err
		}
		files++
		return nil
	}); err != nil {
		return Manifest{}, err
	}
	manifest, err := Digest(repo)
	if err != nil {
		return Manifest{}, err
	}
	manifest.Files = files
	return manifest, nil
}

// verifiedCache memoizes successful verifications for the lifetime of the
// process, keyed by (engine root, expected digest). Verify runs on every
// Spawn, and a full tree hash per poll is wasteful. Trade-off: if the
// on-disk tree is modified after a successful verification, this process
// will not notice until restart. That is acceptable because the cache only
// guards against crash/partial-write corruption at unpack time, not against
// a concurrent writer with access to the tree.
var (
	verifiedMu    sync.Mutex
	verifiedCache = map[verifiedKey]Manifest{}
)

type verifiedKey struct {
	root   string
	digest string
}

func Verify(repo, expected string) (Manifest, error) {
	key := verifiedKey{root: filepath.Join(repo, RootRel), digest: expected}
	verifiedMu.Lock()
	cached, ok := verifiedCache[key]
	verifiedMu.Unlock()
	if ok {
		return cached, nil
	}
	manifest, err := Digest(repo)
	if err != nil {
		return Manifest{}, err
	}
	if manifest.Digest != expected {
		return manifest, fmt.Errorf("embedded engine digest mismatch: expected %s found %s", expected, manifest.Digest)
	}
	verifiedMu.Lock()
	verifiedCache[key] = manifest
	verifiedMu.Unlock()
	return manifest, nil
}

func Digest(repo string) (Manifest, error) {
	root := filepath.Join(repo, RootRel)
	hash := sha256.New()
	files := 0
	if err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		// Python bytecode is interpreter-generated runtime state, not engine
		// content: any import of the tree may write __pycache__/*.pyc, so
		// hashing it would make the digest diverge from the unpack-time
		// manifest the moment the engine runs.
		if d.IsDir() {
			if d.Name() == "__pycache__" {
				return filepath.SkipDir
			}
			return nil
		}
		switch filepath.Ext(d.Name()) {
		case ".pyc", ".pyo":
			return nil
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		hash.Write([]byte(filepath.ToSlash(rel)))
		hash.Write([]byte{0})
		hash.Write(data)
		hash.Write([]byte{0})
		files++
		return nil
	}); err != nil {
		return Manifest{}, err
	}
	return Manifest{Root: RootRel, Digest: hex.EncodeToString(hash.Sum(nil)), Files: files}, nil
}

func PythonPath(repo string) string {
	return filepath.Join(repo, RootRel)
}

func writeFile(path string, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".tmp-*")
	if err != nil {
		return err
	}
	name := tmp.Name()
	ok := false
	defer func() {
		if !ok {
			_ = os.Remove(name)
		}
	}()
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Chmod(0o644); err != nil {
		_ = tmp.Close()
		return err
	}
	// Sync before rename so a crash cannot leave a renamed-but-empty file;
	// digest verification depends on these files being intact.
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(name, path); err != nil {
		return err
	}
	ok = true
	return nil
}
