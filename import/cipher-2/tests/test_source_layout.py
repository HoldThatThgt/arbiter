import unittest
from pathlib import Path


class SourceLayoutTests(unittest.TestCase):
    def test_src_python_files_remain_under_loc_limit(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        src_root = repo_root / "src"
        failures = []
        for path in sorted(src_root.rglob("*.py")):
            with path.open("r", encoding="utf-8") as handle:
                line_count = sum(1 for _line in handle)
            if line_count >= 2000:
                failures.append(f"{path.relative_to(repo_root)} has {line_count} LOC")
        self.assertEqual([], failures, "src/ Python files must stay below 2000 LOC")


if __name__ == "__main__":
    unittest.main()
