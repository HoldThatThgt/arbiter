import concurrent.futures
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from arbiter_engine.runs import recipes
from arbiter_engine.runs import runner


class RunnerContentionTest(unittest.TestCase):
    def test_eight_way_compile_stage_serializes_same_workdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "worker.py"
            worker.write_text(
                textwrap.dedent(
                    """\
                    import fcntl
                    import json
                    import time
                    from pathlib import Path

                    state_path = Path("active.json")
                    lock_path = Path("active.lock")
                    with lock_path.open("a+") as lock:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                        if state_path.exists():
                            state = json.loads(state_path.read_text(encoding="utf-8"))
                        else:
                            state = {"active": 0, "max": 0}
                        state["active"] += 1
                        state["max"] = max(state["max"], state["active"])
                        state_path.write_text(json.dumps(state), encoding="utf-8")
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                    time.sleep(0.03)
                    with lock_path.open("a+") as lock:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                        state = json.loads(state_path.read_text(encoding="utf-8"))
                        state["active"] -= 1
                        state_path.write_text(json.dumps(state), encoding="utf-8")
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                    """
                ),
                encoding="utf-8",
            )
            book = recipes.parse(
                f"""
targets:
  - id: unit
    binary: build/unit
    workdir: .
    harness:
      kind: gtest
    src_compile:
      cmd: [{sys.executable}, {str(worker)}]
"""
            )

            def run_once(_):
                return runner.run_stage(root, book, "unit", "src_compile")

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(run_once, range(8)))

            self.assertEqual([result.exit_code for result in results], [0] * 8)
            state = json.loads((root / "active.json").read_text(encoding="utf-8"))
            self.assertEqual(state["max"], 1)


if __name__ == "__main__":
    unittest.main()
