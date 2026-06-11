package deploy

import (
	"path/filepath"
	"testing"
)

func TestEmbeddedEngineInitUnpacksAndRecordsDigest(t *testing.T) {
	root := t.TempDir()
	opts := testInitOptions()
	opts.EmbeddedEngine = true
	if _, err := InitWithOptions(root, opts); err != nil {
		t.Fatal(err)
	}
	if _, err := filepath.Abs(filepath.Join(root, ".arbiter", "engine", "arbiter_engine", "__init__.py")); err != nil {
		t.Fatal(err)
	}
	if got := readText(t, filepath.Join(root, ".arbiter", "engine", "arbiter_engine", "__init__.py")); got == "" {
		t.Fatal("embedded engine package was not unpacked")
	}
	var engines map[string]any
	readJSONFile(t, filepath.Join(root, ".arbiter", "run", "engines.json"), &engines)
	if engines["mode"] != "embedded" || engines["engine_root"] != ".arbiter/engine" {
		t.Fatalf("engines.json = %#v", engines)
	}
	if digest, _ := engines["engine_digest"].(string); len(digest) != 64 {
		t.Fatalf("engine_digest = %#v", engines["engine_digest"])
	}
}
