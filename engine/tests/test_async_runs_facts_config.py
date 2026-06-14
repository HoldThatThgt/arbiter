import tempfile
import unittest
from pathlib import Path

from arbiter_engine.runs import async_runs


class FactsConfigForRunTest(unittest.TestCase):
    """The async run worker must consume facts.index_on_build.{key_flags,pool}
    from committed config so the documented knobs actually reach extraction."""

    def test_reads_key_flags_and_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".arbiter").mkdir()
            (repo / ".arbiter" / "config.yml").write_text(
                "facts:\n  index_on_build:\n    pool: 3\n    key_flags: [-O2, -g]\n",
                encoding="utf-8",
            )
            facts = async_runs._facts_config(repo)

        self.assertEqual(facts.index_on_build.pool, 3)
        self.assertEqual(facts.index_on_build.key_flags, ("-O2", "-g"))

    def test_missing_config_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            facts = async_runs._facts_config(Path(tmp))

        self.assertIsNone(facts.index_on_build.pool)
        self.assertEqual(facts.index_on_build.key_flags, ())

    def test_malformed_config_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".arbiter").mkdir()
            (repo / ".arbiter" / "config.yml").write_text(
                "facts:\n  bogus_key: true\n", encoding="utf-8"
            )
            facts = async_runs._facts_config(repo)

        self.assertIsNone(facts.index_on_build.pool)
        self.assertEqual(facts.index_on_build.key_flags, ())


if __name__ == "__main__":
    unittest.main()
