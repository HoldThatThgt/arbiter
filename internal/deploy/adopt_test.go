package deploy

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestAdoptMigratesLegacyFixtures(t *testing.T) {
	root := t.TempDir()
	writeText(t, filepath.Join(root, ".chess", "playbook", "flow.md"), "playbook-body\n")
	writeText(t, filepath.Join(root, ".crun-mcp", "recipes.yaml"), "targets:\n  - id: unit\n    binary: ./unit\n")
	writeText(t, filepath.Join(root, ".cipher", "config.yml"), "extractor:\n  worker_count: 4\nincremental:\n  enabled: true\n")
	writeText(t, filepath.Join(root, ".chess", "run", "state.json"), "{}\n")
	writeText(t, filepath.Join(root, ".cipher", "snapshots", "current", "manifest.json"), "{}\n")
	writeText(t, filepath.Join(root, ".crun-mcp", "run", "state.sqlite"), "derived\n")
	writeText(t, filepath.Join(root, "README.md"), "Use LoadPlayBook with crun-mcp.\npreLoadPlayBookSuffix is not a hit.\n")
	writeJSONFile(t, filepath.Join(root, fileMCP), map[string]any{
		"mcpServers": map[string]any{
			"chess":    map[string]any{"type": "stdio", "command": "chess"},
			"cipher-2": map[string]any{"type": "stdio", "command": "python3"},
			"crun-mcp": map[string]any{"type": "stdio", "command": "uvx"},
			"foreign":  map[string]any{"type": "stdio", "command": "keep"},
		},
	})

	report, err := Adopt(root)
	if err != nil {
		t.Fatal(err)
	}
	if readText(t, filepath.Join(root, ".arbiter", "playbook", "flow.md")) != "playbook-body\n" {
		t.Fatal("playbook was not migrated")
	}
	if _, err := os.Stat(filepath.Join(root, ".chess", "playbook", "flow.md")); !os.IsNotExist(err) {
		t.Fatalf("legacy playbook still exists or stat failed: %v", err)
	}
	recipes := readText(t, filepath.Join(root, ".arbiter", "recipes.yaml"))
	if !strings.Contains(recipes, "Migrated from .crun-mcp/recipes.yaml") || !strings.Contains(recipes, "binary: ./unit") {
		t.Fatalf("recipes.yaml = %q", recipes)
	}
	config := readText(t, filepath.Join(root, ".arbiter", "config.yml"))
	for _, want := range []string{"facts:", "incremental: true", "pool: 4", "# extractor:"} {
		if !strings.Contains(config, want) {
			t.Fatalf("config missing %q:\n%s", want, config)
		}
	}
	for _, path := range []string{".chess/run", ".cipher/snapshots", ".crun-mcp/run"} {
		if _, err := os.Stat(filepath.Join(root, path)); !os.IsNotExist(err) {
			t.Fatalf("derived path %s still exists or stat failed: %v", path, err)
		}
	}
	var mcp map[string]any
	readJSONFile(t, filepath.Join(root, fileMCP), &mcp)
	servers := mcp["mcpServers"].(map[string]any)
	if _, ok := servers["foreign"]; !ok || len(servers) != 1 {
		t.Fatalf("mcp servers = %#v", servers)
	}
	if len(report.Checklist) != 2 {
		t.Fatalf("checklist = %#v", report.Checklist)
	}
	if !strings.Contains(report.String(), "README.md:1 LoadPlayBook") {
		t.Fatalf("report string = %q", report.String())
	}
}

func TestAdoptIsIdempotentAndDoesNotRewriteChecklistFiles(t *testing.T) {
	root := t.TempDir()
	writeText(t, filepath.Join(root, ".chess", "playbook", "flow.md"), "playbook-body\n")
	writeText(t, filepath.Join(root, "docs", "notes.md"), "crun-mcp remains manual.\n")
	if _, err := Adopt(root); err != nil {
		t.Fatal(err)
	}
	before := snapshot(t, root)
	if _, err := Adopt(root); err != nil {
		t.Fatal(err)
	}
	after := snapshot(t, root)
	if len(before) != len(after) {
		t.Fatalf("snapshot size changed: %d -> %d", len(before), len(after))
	}
	for path, data := range before {
		if string(after[path]) != string(data) {
			t.Fatalf("file changed on second adopt: %s", path)
		}
	}
	if got := readText(t, filepath.Join(root, "docs", "notes.md")); got != "crun-mcp remains manual.\n" {
		t.Fatalf("checklist file was rewritten: %q", got)
	}
}

func TestAdoptCipherConfigHonorsInlineComments(t *testing.T) {
	root := t.TempDir()
	// The old hand-rolled parser fed "8 # eight workers" to Atoi (dropping
	// the setting) and refused "false # disabled" as a bool.
	writeText(t, filepath.Join(root, ".cipher", "config.yml"),
		"extractor:\n  worker_count: 8 # eight workers\nincremental:\n  enabled: false # disabled for now\n")
	if _, err := Adopt(root); err != nil {
		t.Fatal(err)
	}
	config := readText(t, filepath.Join(root, ".arbiter", "config.yml"))
	for _, want := range []string{"pool: 8", "incremental: false"} {
		if !strings.Contains(config, want) {
			t.Fatalf("config missing %q:\n%s", want, config)
		}
	}
}

func TestAdoptCipherConfigDoesNotLeakTabNestedSections(t *testing.T) {
	root := t.TempDir()
	// Tabs are invalid YAML indentation. The old parser misread the
	// tab-indented "extractor:" as a top-level section and leaked
	// worker_count out of a foreign subtree; a real YAML parser must not.
	writeText(t, filepath.Join(root, ".cipher", "config.yml"),
		"wrapper:\n\textractor:\n\t\tworker_count: 9\n")
	if _, err := Adopt(root); err != nil {
		t.Fatal(err)
	}
	config := readText(t, filepath.Join(root, ".arbiter", "config.yml"))
	if strings.Contains(config, "pool:") {
		t.Fatalf("tab-nested worker_count leaked into config:\n%s", config)
	}
	for _, want := range []string{"facts:", "# wrapper:"} {
		if !strings.Contains(config, want) {
			t.Fatalf("config missing %q:\n%s", want, config)
		}
	}
}

func writeText(t *testing.T, path, text string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
		t.Fatal(err)
	}
}
