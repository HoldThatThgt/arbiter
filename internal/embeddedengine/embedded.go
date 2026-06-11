package embeddedengine

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"

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

func Verify(repo, expected string) (Manifest, error) {
	manifest, err := Digest(repo)
	if err != nil {
		return Manifest{}, err
	}
	if manifest.Digest != expected {
		return manifest, fmt.Errorf("embedded engine digest mismatch: expected %s found %s", expected, manifest.Digest)
	}
	return manifest, nil
}

func Digest(repo string) (Manifest, error) {
	root := filepath.Join(repo, RootRel)
	hash := sha256.New()
	files := 0
	if err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
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
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(name, path); err != nil {
		return err
	}
	ok = true
	return nil
}
