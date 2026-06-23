package deploy_test

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/HoldThatThgt/arbiter/internal/deploy"
	"github.com/HoldThatThgt/arbiter/internal/embeddedengine"
	"github.com/HoldThatThgt/arbiter/internal/match"
	"github.com/HoldThatThgt/arbiter/internal/verify"
)

type endgameDemoResult struct {
	InitOK              bool
	IntroOK             bool
	PlayOK              bool
	ProvenRecipes       int
	SnapshotID          string
	Openings            []string
	MacroChecklist      []deploy.ChecklistItem
	ASanRekeyedSnapshot bool
	TailMS              int
	TerminalMatch       string
	TaskVerdicts        []string
}

func TestEndgameDemoFixtureZeroCeremony(t *testing.T) {
	demo := runEndgameDemoFixture(t)

	if !demo.InitOK || !demo.IntroOK || !demo.PlayOK {
		t.Fatalf("demo phases = init:%t intro:%t play:%t", demo.InitOK, demo.IntroOK, demo.PlayOK)
	}
	if demo.ProvenRecipes != 1 || demo.SnapshotID == "" {
		t.Fatalf("intro evidence recipes=%d snapshot=%q", demo.ProvenRecipes, demo.SnapshotID)
	}
	if strings.Join(demo.Openings, ",") != "build-feature,fix-reported-bug,fix-slow-path,freeplay,gold-digger,hunt-latent-bugs,recipe-derivation,regression-triage" {
		t.Fatalf("openings = %#v", demo.Openings)
	}
	if len(demo.MacroChecklist) == 0 {
		t.Fatal("intro macro scan produced no checklist evidence")
	}
	if !demo.ASanRekeyedSnapshot {
		t.Fatalf("asan profile did not re-extract under its own cache key")
	}
	if demo.TailMS < 0 || demo.TailMS > 1000 {
		t.Fatalf("tail_ms = %d, want fixture budget <= 1000", demo.TailMS)
	}
	if demo.TerminalMatch != match.StatusFinishedSuccess {
		t.Fatalf("terminal match = %q", demo.TerminalMatch)
	}
	if len(demo.TaskVerdicts) != 5 {
		t.Fatalf("task verdicts = %#v", demo.TaskVerdicts)
	}
	for _, verdict := range demo.TaskVerdicts {
		if verdict != match.TaskPass {
			t.Fatalf("task verdicts = %#v", demo.TaskVerdicts)
		}
	}
}

func runEndgameDemoFixture(t *testing.T) endgameDemoResult {
	t.Helper()
	root := t.TempDir()
	writeText(t, filepath.Join(root, "src", "fixture.c"), "int fixture(void) { return 1; }\n#ifdef __SANITIZE_ADDRESS__\n#endif\n")
	writeText(t, filepath.Join(root, "src", "bug.c"), "int broken(void) { return 0; }\n")
	if git, err := exec.LookPath("git"); err == nil {
		cmd := exec.Command(git, "init", "-q", root)
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git init: %v\n%s", err, out)
		}
	}

	opts := deploy.Options{
		Python: "python3",
		Now:    func() time.Time { return time.Date(2026, 6, 11, 0, 0, 0, 0, time.UTC) },
		VerifyEngine: func(string, string) (string, error) {
			version, err := embeddedengine.Version()
			if err != nil {
				return "", err
			}
			return version, nil
		},
		VerifyCompanions: func(string) error { return nil },
		FSKind:           "apfs",
	}
	if _, err := deploy.InitWithOptions(root, opts); err != nil {
		t.Fatal(err)
	}
	macros, err := deploy.ScanInstrumentationMacros(root)
	if err != nil {
		t.Fatal(err)
	}
	probe := runIntroRecipeProbe(t, root)
	if !probe.PlainPublished || !probe.ASanPublished {
		t.Fatalf("intro probe did not publish facts: %#v", probe)
	}
	// Sanitizer flags are semantic (they change preprocessor state), so the
	// asan profile must publish its own snapshot from a fresh extraction
	// instead of reusing the plain build's cache entries.
	if probe.SameSnapshot || probe.Extractions != 2 {
		t.Fatalf("asan unexpectedly reused plain extraction: %#v", probe)
	}

	verdicts, terminal := runFreeplayFix(t, root)
	return endgameDemoResult{
		InitOK:              fileExists(filepath.Join(root, ".claude", "skills", "arbiter-intro", "SKILL.md")),
		IntroOK:             probe.PlainPublished && len(macros.Checklist) > 0,
		PlayOK:              terminal == match.StatusFinishedSuccess,
		ProvenRecipes:       1,
		SnapshotID:          probe.SnapshotID,
		Openings:            deployedOpenings(t, root),
		MacroChecklist:      macros.Checklist,
		ASanRekeyedSnapshot: !probe.SameSnapshot && probe.Extractions == 2,
		TailMS:              probe.TailMS,
		TerminalMatch:       terminal,
		TaskVerdicts:        verdicts,
	}
}

type recipeProbe struct {
	PlainPublished bool   `json:"plain_published"`
	ASanPublished  bool   `json:"asan_published"`
	SameSnapshot   bool   `json:"same_snapshot"`
	SnapshotID     string `json:"snapshot_id"`
	Extractions    int    `json:"extractions"`
	TailMS         int    `json:"tail_ms"`
	PlainOverall   string `json:"plain_overall"`
	PlainFailure   string `json:"plain_failure"`
	PlainStderr    string `json:"plain_stderr"`
	ASanOverall    string `json:"asan_overall"`
	ASanFailure    string `json:"asan_failure"`
	ASanStderr     string `json:"asan_stderr"`
}

func runIntroRecipeProbe(t *testing.T, root string) recipeProbe {
	t.Helper()
	module := moduleRoot(t)
	script := filepath.Join(root, "intro_probe.py")
	writeText(t, script, introProbeScript)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "python3", script, root)
	cmd.Dir = root
	env := os.Environ()
	// engine/ for arbiter_engine, engine/tests for the c2 test package (importing it installs the
	// in-process JSON libclang backend so the probe extracts hermetically, no real libclang in CI).
	enginePath := filepath.Join(module, "engine")
	testsPath := filepath.Join(module, "engine", "tests")
	env = append(env, "PYTHONPATH="+enginePath+string(os.PathListSeparator)+testsPath+string(os.PathListSeparator)+os.Getenv("PYTHONPATH"))
	cmd.Env = env
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("intro probe: %v\n%s", err, out)
	}
	var probe recipeProbe
	if err := json.Unmarshal(out, &probe); err != nil {
		t.Fatalf("intro probe json: %v\n%s", err, out)
	}
	return probe
}

func runFreeplayFix(t *testing.T, root string) ([]string, string) {
	t.Helper()
	// freeplay's gear-up step is now [Submit]-bound to the curated gear-up-published
	// predicate (run: src_compile, expect facts.published), so it no longer accepts an
	// inline shell — the player must drive a real cc-interposed src_compile run whose
	// facts publish. Seed a hermetic build gate the referee's engine run can publish from
	// (a fake clang pinned via facts.toolchain + the in-process JSON libclang backend), so
	// this fixture proves the bound gate end to end instead of asserting a snapshot file.
	seedFreeplayBuildGate(t, root)

	store := match.New(root, "player")
	if _, err := store.LoadPlayBook("freeplay"); err != nil {
		t.Fatal(err)
	}
	steps := []struct {
		request string
		// Exactly one of verify (a [Submit]-bound curated predicate) or command (an inline
		// shell predicate) drives the step; gear-up is bound, the rest are inline.
		verify  string
		command string
		before  func()
	}{
		{"gear up from the proven src_compile snapshot", "gear-up-published", "", nil},
		{"orient on the broken fixture and snapshot evidence", "", "grep -q 'int broken' src/bug.c && grep -q 'sha256-' .arbiter/facts/snapshots/current", nil},
		{"plan one refereed fixture edit", "", "test -f .arbiter/playbook/freeplay.md && test -f .arbiter/recipes.yaml", nil},
		{"fix broken fixture return value", "", "grep -q 'return 1;' src/bug.c", func() {
			writeText(t, filepath.Join(root, "src", "bug.c"), "int broken(void) { return 1; }\n")
		}},
		{"record the terminal evidence", "", "test -s src/bug.c", nil},
	}
	verdicts := make([]string, 0, len(steps))
	terminal := ""
	for _, step := range steps {
		if _, err := store.ShowStepJob(); err != nil {
			t.Fatal(err)
		}
		task, err := store.CreateTask(step.request)
		if err != nil {
			t.Fatal(err)
		}
		if step.before != nil {
			step.before()
		}
		spec := verify.ResultSpec{Kind: "shell", Command: step.command}
		if step.verify != "" {
			spec = verify.ResultSpec{Verify: step.verify}
		}
		submitted, err := store.SubmitTask(context.Background(), task.TaskID, "predicate passed", step.request, spec)
		if err != nil {
			t.Fatal(err)
		}
		verdicts = append(verdicts, submitted.Verdict)
		check, err := store.CheckStepJob(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		if check.Match != "" {
			terminal = check.Match
		}
	}
	return verdicts, terminal
}

// seedFreeplayBuildGate materializes a hermetic src_compile build gate the referee's
// engine `run` can publish facts from, so freeplay's [Submit]-bound gear-up step
// (gear-up-published → run: src_compile, expect facts.published) passes in-process.
//
// The engine `run` tool resolves its extractor toolchain from .arbiter/config.yml's
// facts.toolchain (it cannot be handed a fake ExtractorConfig the way the out-of-band
// probe is), so we pin facts.toolchain.clang to the same JSON-AST fake clang the probe
// uses and install the in-process JSON libclang backend in the spawned engine via a
// sitecustomize.py that imports the c2 test package. The recipe needs only a top-level
// compile_db, a trivially green src_compile, and real `sources:` — publish recovers and
// indexes the journaled/recovered TUs through the JSON backend without a real toolchain.
func seedFreeplayBuildGate(t *testing.T, root string) {
	t.Helper()
	module := moduleRoot(t)
	// engine/ for arbiter_engine, engine/tests for the c2 test package, .arbiter/site for
	// the sitecustomize.py that imports it (installing the in-process JSON libclang
	// backend). Preserve any host PYTHONPATH on the end — same contract runIntroRecipeProbe
	// uses — so a box that needs deps on PYTHONPATH keeps them for both extraction paths.
	host := os.Getenv("PYTHONPATH")
	parts := []string{
		filepath.Join(module, "engine"),
		filepath.Join(module, "engine", "tests"),
		filepath.Join(root, ".arbiter", "site"),
	}
	if host != "" {
		parts = append(parts, host)
	}
	enginePythonPath := strings.Join(parts, string(os.PathListSeparator))

	script := filepath.Join(root, "freeplay_gate_setup.py")
	writeText(t, script, freeplayGateSetupScript)
	cmd := exec.Command("python3", script, root)
	cmd.Dir = root
	cmd.Env = append(os.Environ(), "PYTHONPATH="+enginePythonPath)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("seed freeplay build gate: %v\n%s", err, out)
	}
	// The match-store engine (engineclient.Spawn RoleExec, cwd=root) sets
	// PYTHONPATH=<root>/engine + this inherited value, so the run-predicate spawn resolves
	// the engine source, the c2 backend, and the host deps the same way the setup did.
	t.Setenv("PYTHONPATH", enginePythonPath)
}

// freeplayGateSetupScript writes, under root: the JSON-AST fake clang (from the c2 test
// helper, so it satisfies the indexer's capability probe), a facts.toolchain pin at it, a
// compile_commands.json for the real source, a publishable src_compile recipe, a fake
// gtest binary, and a sitecustomize.py that installs the in-process JSON libclang backend.
const freeplayGateSetupScript = `import sys
from pathlib import Path

from c2.toolchain_helpers import _fake_clang_script

root = Path(sys.argv[1])
(root / "src").mkdir(parents=True, exist_ok=True)
(root / "src" / "fixture.c").write_text("int fixture(void){return 1;}\n", encoding="utf-8")

toolchain = root / ".arbiter" / "toolchain"
toolchain.mkdir(parents=True, exist_ok=True)
clang = toolchain / "clang"
clang.write_text(_fake_clang_script("16.0.6"), encoding="utf-8")
clang.chmod(0o755)

(root / ".arbiter" / "config.yml").write_text(
    "facts:\n  toolchain:\n    clang: %s\n" % clang, encoding="utf-8"
)

src = root / "src" / "fixture.c"
(root / "compile_commands.json").write_text(
    '[{"directory":"%s","file":"%s","arguments":["%s","-c","%s"]}]\n' % (root, src, clang, src),
    encoding="utf-8",
)

gtest = root / "fake_gtest.sh"
gtest.write_text(
    '#!/bin/sh\n'
    'for arg in "$@"; do case "$arg" in --gtest_output=xml:*) out="${arg#--gtest_output=xml:}" ;; esac; done\n'
    'mkdir -p "$(dirname "$out")"\n'
    'cat > "$out" <<XML\n'
    '<testsuites tests="1" failures="0"><testsuite name="Suite"><testcase classname="Suite" name="Pass" time="0.001"/></testsuite></testsuites>\n'
    'XML\n',
    encoding="utf-8",
)
gtest.chmod(0o755)

(root / ".arbiter" / "recipes.yaml").write_text(
    "compile_db:\n  path: compile_commands.json\n"
    "targets:\n"
    "  - id: src_compile\n"
    "    binary: src/fixture.c\n"
    "    harness:\n      kind: gtest\n"
    '    src_compile:\n      cmd: ["true"]\n'
    '    test_run:\n      cmd: ["%s"]\n'
    "    sources: [src/*.c]\n" % gtest,
    encoding="utf-8",
)

site = root / ".arbiter" / "site"
site.mkdir(parents=True, exist_ok=True)
(site / "sitecustomize.py").write_text(
    "try:\n"
    "    import c2  # installs the in-process JSON libclang backend as an import side effect\n"
    "except Exception as exc:  # pragma: no cover - surfaced via the failing run predicate\n"
    "    import sys\n"
    "    sys.stderr.write('freeplay gate sitecustomize: c2 import failed: %r\\n' % exc)\n",
    encoding="utf-8",
)
`

func deployedOpenings(t *testing.T, root string) []string {
	t.Helper()
	entries, err := os.ReadDir(filepath.Join(root, ".arbiter", "playbook"))
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, entry := range entries {
		if entry.IsDir() || filepath.Ext(entry.Name()) != ".md" {
			continue
		}
		if entry.Name() == "FORMAT.md" {
			continue
		}
		names = append(names, strings.TrimSuffix(entry.Name(), ".md"))
	}
	sort.Strings(names)
	return names
}

func moduleRoot(t *testing.T) string {
	t.Helper()
	wd, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	for {
		if fileExists(filepath.Join(wd, "go.mod")) {
			return wd
		}
		parent := filepath.Dir(wd)
		if parent == wd {
			t.Fatal("go.mod not found")
		}
		wd = parent
	}
}

func fileExists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
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

const introProbeScript = `#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

from arbiter_engine.runs import gtest
from arbiter_engine.runs import recipes

# Importing the c2 test package installs the in-process JSON libclang backend; write_fake_toolchain
# returns an ExtractorConfig pointing at a fake clang/gcc so extraction needs no real libclang.
from c2.toolchain_helpers import write_fake_toolchain

root = Path(sys.argv[1])
fake_arbiter = root / "fake_arbiter.py"
fake_cc = root / "fake_cc.sh"
fake_gtest = root / "fake_gtest.sh"

fake_arbiter.write_text("""#!/usr/bin/env python3
import json
import os
import subprocess
import sys

if len(sys.argv) < 4 or sys.argv[1:3] != ["cc", "--"]:
    sys.exit(2)
argv = sys.argv[3:]
src = ""
out = ""
for index, arg in enumerate(argv):
    if arg.endswith((".c", ".cc", ".cpp", ".cxx")) and not src:
        src = arg
    if arg == "-o" and index + 1 < len(argv):
        out = argv[index + 1]
path = os.path.join(
    os.getcwd(),
    ".arbiter",
    "facts",
    "run",
    "compile-journal.%s.jsonl" % os.environ.get("ARBITER_BUILD_ID", "default"),
)
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "src": src, "out": out}, separators=(",", ":")) + "\\n")
sys.exit(subprocess.run(argv).returncode)
""", encoding="utf-8")
fake_arbiter.chmod(0o755)

fake_cc.write_text("""#!/bin/sh
printf '%s\\n' "$CFLAGS" >> cflags.log
mkdir -p build
touch build/fixture.o
""", encoding="utf-8")
fake_cc.chmod(0o755)

fake_gtest.write_text("""#!/bin/sh
for arg in "$@"; do
  case "$arg" in --gtest_output=xml:*) out="${arg#--gtest_output=xml:}" ;; esac
done
mkdir -p "$(dirname "$out")"
cat > "$out" <<'XML'
<testsuites tests="1" failures="0"><testsuite name="Suite"><testcase classname="Suite" name="Pass" time="0.001"/></testsuite></testsuites>
XML
""", encoding="utf-8")
fake_gtest.chmod(0o755)

recipe_path = root / ".arbiter" / "recipes.yaml"
recipe_path.write_text(f"""
profiles:
  asan:
    cflags_append: [-fsanitize=address]
compile_db:
  path: compile_commands.json
targets:
  - id: src_compile
    binary: build/unit
    harness:
      kind: gtest
    src_compile:
      cmd: [/bin/sh, -c, "$CC $CFLAGS -Iinclude -O2 -c src/fixture.c -o build/fixture.o"]
      env:
        CC: {fake_cc}
    test_run:
      cmd: [{fake_gtest}]
""", encoding="utf-8")
book = recipes.load(recipe_path)
# A fake clang/gcc + the in-process JSON libclang backend (installed by importing c2) make the
# build-driven extraction hermetic; the compile-db the extractor reads is the journaled one.
config = write_fake_toolchain(root, compile_database_path=root / "compile_commands.json")

plain = gtest.run_target(root, book, "src_compile", run_id="plain", arbiter_bin=str(fake_arbiter), extractor_config=config)
asan = gtest.run_target(root, book, "src_compile", run_id="asan", profiles=["asan"], arbiter_bin=str(fake_arbiter), extractor_config=config)
plain_facts = plain.to_json()["facts"]
asan_facts = asan.to_json()["facts"]
# A sanitizer profile is semantic (it changes preprocessor state), so the asan build must publish its
# own content-addressed snapshot instead of reusing the plain build's. Two distinct snapshot ids ==
# two real extractions (the source id hashes the profile, so default != asan).
distinct_snapshots = {plain_facts["snapshot_id"], asan_facts["snapshot_id"]}
print(json.dumps({
    "plain_published": bool(plain_facts["published"]),
    "asan_published": bool(asan_facts["published"]),
    "same_snapshot": plain_facts["snapshot_id"] == asan_facts["snapshot_id"],
    "snapshot_id": asan_facts["snapshot_id"],
    "extractions": len(distinct_snapshots),
    "tail_ms": int(asan_facts["tail_ms"]),
    "plain_overall": plain.overall,
    "plain_failure": plain.failure or "",
    "plain_stderr": plain.stderr_tail,
    "asan_overall": asan.overall,
    "asan_failure": asan.failure or "",
    "asan_stderr": asan.stderr_tail,
}, sort_keys=True))
`
