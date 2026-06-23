"""End-to-end build-cache wiring through a real run_stage compile.

These tests exercise the production path (runner.run_stage under the build
lock, with `arbiter cc` injection and the census-validated cache) rather than
the build_cache module in isolation. A compile "miss" runs the recipe's
command, which appends a line to a counter file and (re)produces the binary; a
"hit" skips the command entirely, so the counter does not advance and the
compiler is provably not re-invoked.

The behaviours asserted mirror docs/modules/engine-runs.md:43-46 and ADR-0005
("build cache keys on full flags + profile"):
  * an identical rerun over a fixture with `sources:` is a CACHE HIT,
  * editing a source forces a MISS (stale-binary polarity — the bug crun had),
  * changing a compile flag (CFLAGS) or src_compile.cmd forces a MISS even
    though the source census is unchanged — the inputs the census cannot see
    must still be folded into the cache key,
  * a recipe WITHOUT `sources:` never hits cross-process.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from arbiter_engine.runs import recipes
from arbiter_engine.runs import runner


# A compile stage that records each real invocation in compile-count.log and
# (re)produces the target binary. When run_stage serves the stage from cache it
# skips this command, so the log length is a faithful compile counter.
COMPILE_CMD = (
    "/bin/sh",
    "-c",
    'printf "x\\n" >> compile-count.log; mkdir -p build; printf bin > build/app',
)


def _recipe(
    *,
    with_sources: bool,
    cflags: str | None = None,
    cmd: tuple[str, ...] = COMPILE_CMD,
) -> str:
    cmd_text = ", ".join(_quote(part) for part in cmd)
    lines = [
        "targets:",
        "  - id: unit",
        "    binary: build/app",
        "    workdir: .",
        "    harness:",
        "      kind: gtest",
    ]
    if cflags is not None:
        # CFLAGS reaches the compiler through stage env (and so the produced
        # binary), but the source census never sees it — only the cache key can.
        lines.append("    env:")
        lines.append(f"      CFLAGS: {_quote(cflags)}")
    if with_sources:
        lines.append("    sources: [src/**/*.c]")
    lines.append("    src_compile:")
    lines.append(f"      cmd: [{cmd_text}]")
    return "\n".join(lines) + "\n"


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _compile_count(root: Path) -> int:
    log = root / "compile-count.log"
    if not log.exists():
        return 0
    return len([line for line in log.read_text(encoding="utf-8").splitlines() if line])


class BuildCacheIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "a.c").write_text("int a(void){return 0;}\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, book: recipes.RecipeBook) -> runner.StageResult:
        return runner.run_stage(self.root, book, "unit", "src_compile")

    def test_identical_rerun_is_a_cache_hit(self):
        book = recipes.parse(_recipe(with_sources=True))

        first = self._run(book)
        self.assertEqual(first.exit_code, 0)
        self.assertEqual(_compile_count(self.root), 1)
        self.assertTrue((self.root / "build" / "app").exists())

        second = self._run(book)
        self.assertEqual(second.exit_code, 0)
        # The compiler was NOT re-invoked: the cache served the stage.
        self.assertEqual(_compile_count(self.root), 1, "second run must be a cache hit")

    def test_source_edit_forces_a_miss(self):
        book = recipes.parse(_recipe(with_sources=True))

        self.assertEqual(self._run(book).exit_code, 0)
        self.assertEqual(_compile_count(self.root), 1)
        # A clean rerun hits.
        self.assertEqual(self._run(book).exit_code, 0)
        self.assertEqual(_compile_count(self.root), 1)

        # Edit a declared source: the census digest changes, so the next run
        # must rebuild (stale-binary polarity, the bug crun originally had). The
        # new content differs in length, so the miss is detected by the census
        # size check and does not depend on filesystem mtime granularity.
        (self.root / "src" / "a.c").write_text("int a(void){return 1234;}\n", encoding="utf-8")
        third = self._run(book)
        self.assertEqual(third.exit_code, 0)
        self.assertEqual(_compile_count(self.root), 2, "source edit must force a recompile")

        # After the rebuild the new census is cached, so the next run hits again.
        self.assertEqual(self._run(book).exit_code, 0)
        self.assertEqual(_compile_count(self.root), 2)

    def test_new_source_file_forces_a_miss(self):
        book = recipes.parse(_recipe(with_sources=True))

        self.assertEqual(self._run(book).exit_code, 0)
        self.assertEqual(self._run(book).exit_code, 0)
        self.assertEqual(_compile_count(self.root), 1)

        # A brand-new source under the glob is a census "new" entry → miss.
        (self.root / "src" / "b.c").write_text("int b(void){return 0;}\n", encoding="utf-8")
        self.assertEqual(self._run(book).exit_code, 0)
        self.assertEqual(_compile_count(self.root), 2, "new source must force a recompile")

    def test_compile_flag_change_forces_a_miss(self):
        # Regression for the stale-binary verdict bug: changing a compile flag
        # (CFLAGS via env, e.g. -O0 -> -O2) changes the produced binary but is
        # invisible to the source census (.arbiter and flags are excluded). The
        # cache key must fold the effective flags, so the changed-flag run MUST
        # recompile rather than serve the prior, differently-compiled binary.
        debug = recipes.parse(_recipe(with_sources=True, cflags="-O0"))
        self.assertEqual(self._run(debug).exit_code, 0)
        self.assertEqual(_compile_count(self.root), 1)
        # Same flags, untouched sources -> a genuine hit (count stays put).
        self.assertEqual(self._run(debug).exit_code, 0)
        self.assertEqual(_compile_count(self.root), 1, "identical flags must hit")

        # Flip ONLY the optimization flag; sources and binary are unchanged.
        release = recipes.parse(_recipe(with_sources=True, cflags="-O2"))
        self.assertEqual(self._run(release).exit_code, 0)
        self.assertEqual(
            _compile_count(self.root), 2, "a CFLAGS change must force a recompile"
        )
        # The new flag set caches independently; rerunning it now hits.
        self.assertEqual(self._run(release).exit_code, 0)
        self.assertEqual(_compile_count(self.root), 2)
        # And the original flag set is still its own cached entry (no clobber).
        self.assertEqual(self._run(debug).exit_code, 0)
        self.assertEqual(_compile_count(self.root), 2)

    def test_compile_command_change_forces_a_miss(self):
        # Regression: changing src_compile.cmd to a command that produces a
        # different binary must MISS. The command bytes never enter the source
        # census, so only a command-aware cache key prevents the prior binary
        # (e.g. AAA) being served for a recipe that now builds BBB.
        produces_aaa = (
            "/bin/sh",
            "-c",
            'printf "x\\n" >> compile-count.log; mkdir -p build; printf AAA > build/app',
        )
        produces_bbb = (
            "/bin/sh",
            "-c",
            'printf "x\\n" >> compile-count.log; mkdir -p build; printf BBB > build/app',
        )

        first = recipes.parse(_recipe(with_sources=True, cmd=produces_aaa))
        self.assertEqual(self._run(first).exit_code, 0)
        self.assertEqual(_compile_count(self.root), 1)
        self.assertEqual((self.root / "build" / "app").read_text(encoding="utf-8"), "AAA")
        # Identical command, untouched sources -> hit.
        self.assertEqual(self._run(first).exit_code, 0)
        self.assertEqual(_compile_count(self.root), 1, "identical command must hit")

        # Swap the compile command; sources are unchanged.
        second = recipes.parse(_recipe(with_sources=True, cmd=produces_bbb))
        self.assertEqual(self._run(second).exit_code, 0)
        self.assertEqual(
            _compile_count(self.root), 2, "a src_compile.cmd change must force a recompile"
        )
        # The new command actually ran, so the on-disk binary is now BBB, not
        # the stale AAA that the buggy key would have served.
        self.assertEqual((self.root / "build" / "app").read_text(encoding="utf-8"), "BBB")

    def test_recipe_without_sources_never_hits(self):
        book = recipes.parse(_recipe(with_sources=False))

        self.assertEqual(self._run(book).exit_code, 0)
        self.assertEqual(self._run(book).exit_code, 0)
        self.assertEqual(self._run(book).exit_code, 0)
        # No `sources:` ⇒ no cache hit, ever: every run recompiles.
        self.assertEqual(_compile_count(self.root), 3, "no-sources recipe must never hit")

    def test_no_sources_never_hits_across_a_separate_process(self):
        # The cache persists in .arbiter/runs/state.sqlite, so a genuinely
        # separate process could in principle read a hit. Prove the no-sources
        # invariant holds across process boundaries, not just within one.
        book_text = _recipe(with_sources=False)
        (self.root / "recipes.yaml").write_text(book_text, encoding="utf-8")

        # Prime the cache in this process.
        book = recipes.parse(book_text)
        self.assertEqual(self._run(book).exit_code, 0)
        self.assertEqual(_compile_count(self.root), 1)

        driver = textwrap.dedent(
            """\
            import sys
            from pathlib import Path

            from arbiter_engine.runs import recipes, runner

            root = Path(sys.argv[1])
            book = recipes.parse((root / "recipes.yaml").read_text(encoding="utf-8"))
            result = runner.run_stage(root, book, "unit", "src_compile")
            sys.exit(result.exit_code)
            """
        )
        env = {
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        }
        proc = subprocess.run(
            [sys.executable, "-c", driver, str(self.root)],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The separate process recompiled rather than reusing the cached binary.
        self.assertEqual(_compile_count(self.root), 2, proc.stderr)


if __name__ == "__main__":
    unittest.main()
