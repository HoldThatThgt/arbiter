import tempfile
import unittest
from pathlib import Path

from cipher2.config import load_config
from cipher2.tools.log import open_log


class ConfigGraphInferenceTest(unittest.TestCase):
    def test_defaults_do_not_expose_graph_or_inference_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(Path(tmp), observe=False)

        self.assertFalse(hasattr(config, "graph_enabled"))
        self.assertFalse(hasattr(config, "inference_enabled"))
        self.assertNotIn("graph", config.to_mapping())
        self.assertNotIn("inference", config.to_mapping())

    def test_legacy_graph_and_inference_sections_are_ignored_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / ".cipher").mkdir()
            (target / ".cipher" / "config.yml").write_text(
                "schema_version: 1\n"
                "graph:\n"
                "  enabled: true\n"
                "  max_traversal_depth: 8\n"
                "inference:\n"
                "  enabled: true\n"
                "  rule_files:\n"
                "    - .cipher/inference/rules.yml\n",
                encoding="utf-8",
            )

            config = load_config(target)
            events = open_log(target).read_events(channel="config").events
            event = next(item for item in events if item.event_name == "config.load")

        self.assertNotIn("graph", config.to_mapping())
        self.assertNotIn("inference", config.to_mapping())
        self.assertEqual(event.status, "warning")
        self.assertEqual(event.payload["legacy_section_count"], 2)
        self.assertEqual(event.payload["outcome"], "loaded_with_legacy_ignored")


if __name__ == "__main__":
    unittest.main()
