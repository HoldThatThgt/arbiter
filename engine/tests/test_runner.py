import tempfile
import unittest
from pathlib import Path

from arbiter_engine import errors
from arbiter_engine.runs import recipes
from arbiter_engine.runs import runner
from arbiter_engine.shared import locks


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
            with locks.acquire(root, [locks.build_lock(root)], timeout_s=0.2):
                with self.assertRaises(errors.RPCError) as ctx:
                    runner.run_stage(root, book, "unit", "test_run", lock_timeout_s=0.05)

            self.assertEqual(ctx.exception.data["kind"], "lock_timeout")
            self.assertTrue(ctx.exception.data["lock"].startswith("build/"))

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
