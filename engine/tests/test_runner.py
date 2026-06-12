import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from arbiter_engine import errors
from arbiter_engine.runs import recipes
from arbiter_engine.runs import runner


RUNNER_RECIPE = """
profiles:
  asan:
    cflags_append: [-fsanitize=address]
    env:
      MODE: asan
targets:
  - id: unit
    binary: build/unit
    workdir: .
    env:
      BASE: target
    harness:
      kind: gtest
    src_compile:
      pre:
        - [/bin/sh, -c, "printf pre >> order.txt"]
      cmd: [/bin/sh, -c, "printf ':cmd:%s:%s:%s:%s' \\"$MODE\\" \\"$CFLAGS\\" \\"$CC\\" \\"$BASE\\" >> order.txt"]
      post:
        - [/bin/sh, -c, "printf ':post' >> order.txt"]
      env:
        CC: clang
    test_run:
      cmd: [/bin/sh, -c, "printf run >> run.txt"]
"""


class RunnerTest(unittest.TestCase):
    def test_stage_order_profile_overlay_and_cc_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book = recipes.parse(RUNNER_RECIPE)

            result = runner.run_stage(
                root,
                book,
                "unit",
                "src_compile",
                profiles=["asan"],
                arbiter_bin="/opt/arbiter",
            )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(
                (root / "order.txt").read_text(encoding="utf-8"),
                "pre:cmd:asan:-fsanitize=address:/opt/arbiter cc -- clang:target:post",
            )

    def test_non_compile_stage_does_not_inject_cc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book = recipes.parse(RUNNER_RECIPE)

            result = runner.run_stage(root, book, "unit", "test_run", arbiter_bin="/opt/arbiter")

            self.assertEqual(result.exit_code, 0)
            self.assertEqual((root / "run.txt").read_text(encoding="utf-8"), "run")

    def test_build_lock_timeout_is_typed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book = recipes.parse(RUNNER_RECIPE)
            # The lock must be held by another process: BSD/darwin flock does
            # not conflict between fds of the same process. The holder resolves
            # the root like resolve_workdir() so the build-lock keys match.
            holder_src = textwrap.dedent(
                """\
                import sys
                from pathlib import Path

                from arbiter_engine.shared import locks

                root = Path(sys.argv[1]).resolve()
                with locks.acquire(root, [locks.build_lock(root)], timeout_s=5):
                    print("ready", flush=True)
                    sys.stdin.read()
                """
            )
            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join(
                filter(None, [str(Path(__file__).resolve().parents[1]), env.get("PYTHONPATH")])
            )
            with subprocess.Popen(
                [sys.executable, "-c", holder_src, os.fspath(root)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                env=env,
            ) as holder:
                self.assertEqual(holder.stdout.readline(), b"ready\n")
                with self.assertRaises(errors.RPCError) as ctx:
                    runner.run_stage(root, book, "unit", "test_run", lock_timeout_s=0.05)

            self.assertEqual(ctx.exception.data["kind"], "lock_timeout")
            self.assertTrue(ctx.exception.data["lock"].startswith("build/"))

    def test_arbiter_bin_resolves_from_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book = recipes.parse(
                """
targets:
  - id: unit
    binary: build/unit
    harness:
      kind: gtest
    src_compile:
      cmd: [/bin/sh, -c, "printf '%s' \\"$CC\\" > cc.txt"]
"""
            )

            with mock.patch.dict(os.environ, {"ARBITER_BIN": "/abs/path/arbiter"}):
                explicit = runner.resolve_arbiter_bin("/explicit/arbiter")
                from_env = runner.resolve_arbiter_bin(None)
                result = runner.run_stage(root, book, "unit", "src_compile")

            self.assertEqual(explicit, "/explicit/arbiter")
            self.assertEqual(from_env, "/abs/path/arbiter")
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(
                (root / "cc.txt").read_text(encoding="utf-8"),
                "/abs/path/arbiter cc -- cc",
            )

        env = dict(os.environ)
        env.pop("ARBITER_BIN", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(runner.resolve_arbiter_bin(None), "arbiter")

    def test_workdir_escape_raises_runner_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            book = recipes.parse(
                """
targets:
  - id: unit
    binary: build/unit
    workdir: ../outside
    harness:
      kind: gtest
    test_run:
      cmd: [/bin/sh, -c, "true"]
"""
            )

            with self.assertRaisesRegex(runner.RunnerError, "escapes the repo root"):
                runner.run_stage(root, book, "unit", "test_run")
            self.assertFalse((Path(tmp) / "outside").exists())

    def test_stale_compile_journal_is_removed_before_compile_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / ".arbiter" / "facts" / "run" / "compile-journal.unit-src_compile.jsonl"
            journal.parent.mkdir(parents=True)
            journal.write_text('{"miss":true,"stale":"previous-build"}\n', encoding="utf-8")
            book = recipes.parse(
                """
targets:
  - id: unit
    binary: build/unit
    harness:
      kind: gtest
    src_compile:
      cmd: [/bin/sh, -c, "true"]
    test_run:
      cmd: [/bin/sh, -c, "true"]
"""
            )

            result = runner.run_stage(root, book, "unit", "src_compile")

            self.assertEqual(result.exit_code, 0)
            self.assertFalse(journal.exists(), "stale journal must not survive a new build")

    def test_non_compile_stage_keeps_existing_journals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / ".arbiter" / "facts" / "run" / "compile-journal.unit-src_compile.jsonl"
            journal.parent.mkdir(parents=True)
            journal.write_text('{"argv":["cc"]}\n', encoding="utf-8")
            book = recipes.parse(RUNNER_RECIPE)

            result = runner.run_stage(root, book, "unit", "test_run")

            self.assertEqual(result.exit_code, 0)
            self.assertTrue(journal.exists())

    def test_env_secret_names_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book = recipes.parse(
                """
targets:
  - id: bad
    binary: build/bad
    env:
      API_TOKEN: nope
    harness:
      kind: gtest
    test_run:
      cmd: [/bin/sh, -c, "true"]
"""
            )

            with self.assertRaisesRegex(runner.RunnerError, "secret-shaped env"):
                runner.run_stage(root, book, "bad", "test_run")


if __name__ == "__main__":
    unittest.main()
