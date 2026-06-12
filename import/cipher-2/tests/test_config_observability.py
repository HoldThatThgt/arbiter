import tempfile
import unittest
from pathlib import Path

from cipher2.config import ConfigError, load_config, write_default_config
from cipher2.tools.log import open_log
from cipher2.tools.views import build_overview


class ConfigObservabilityTest(unittest.TestCase):
    def test_write_default_config_writes_config_write_without_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            compile_db = target / "build" / "compile_commands.json"
            compile_db.parent.mkdir()
            compile_db.write_text("[]", encoding="utf-8")

            write_default_config(target, compile_database="build/compile_commands.json")

            event = next(item for item in open_log(target).read_events(channel="config").events if item.event_name == "config.write")
            self.assertEqual(event.status, "ok")
            self.assertEqual(event.payload["operation"], "write_default_config")
            self.assertEqual(event.payload["outcome"], "written")
            self.assertEqual(event.payload["has_compile_database"], True)
            self.assertEqual(event.payload["compile_database_scope"], "relative")
            self.assertEqual(event.payload["clang_executable_scope"], "none")
            self.assertEqual(event.payload["gcc_executable_scope"], "none")
            self.assertEqual(event.payload["clang_arg_count"], 0)
            self.assertNotIn(str(target), str(event.to_json()))
            self.assertNotIn(str(compile_db), str(event.to_json()))

    def test_load_config_writes_loaded_event_and_views_log_exposes_config_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            compile_db = target / "build" / "compile_commands.json"
            compile_db.parent.mkdir()
            compile_db.write_text("[]", encoding="utf-8")
            write_default_config(target, compile_database="build/compile_commands.json", observe=False)

            load_config(target)

            events = open_log(target).read_events(channel="config").events
            load_event = next(item for item in events if item.event_name == "config.load")
            self.assertEqual(load_event.status, "ok")
            self.assertEqual(load_event.payload["outcome"], "loaded")
            self.assertEqual(load_event.payload["has_compile_database"], True)
            self.assertEqual(load_event.payload["compile_database_scope"], "relative")
            self.assertEqual(load_event.payload["clang_executable_scope"], "none")
            self.assertEqual(load_event.payload["gcc_executable_scope"], "none")
            self.assertEqual(load_event.payload["clang_arg_count"], 0)
            self.assertEqual(load_event.payload["config_exists"], True)

            overview = build_overview(target, include_sections=["log"], top_n=5)
            self.assertIsNotNone(overview.log)
            self.assertEqual(overview.log.events_by_channel["config"], 1)
            self.assertIn(("config.load", 1), overview.log.top_event_names)
            recent = next(row for row in overview.log.recent_events if row.label == "config.load")
            self.assertIn(("operation", "load_config"), recent.fields)
            self.assertIn(("outcome", "loaded"), recent.fields)
            self.assertIn(("has_compile_database", "True"), recent.fields)
            self.assertIn(("clang_executable_scope", "none"), recent.fields)
            self.assertIn(("gcc_executable_scope", "none"), recent.fields)

    def test_invalid_config_writes_config_error_with_stable_code_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / ".cipher").mkdir()
            (target / ".cipher" / "config.yml").write_text(
                "schema_version: 2\npaths:\n  compile_database:\n",
                encoding="utf-8",
            )

            with self.assertRaises(ConfigError):
                load_config(target)

            event = next(item for item in open_log(target).read_events(channel="config").events if item.event_name == "config.error")
            self.assertEqual(event.status, "error")
            self.assertEqual(event.error_code, "unsupported_schema_version")
            self.assertEqual(event.payload["outcome"], "failed")
            self.assertEqual(event.payload["error_code"], "unsupported_schema_version")
            self.assertNotIn("Traceback", str(event.to_json()))

    def test_log_write_failure_does_not_break_config_main_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / ".cipher").mkdir()
            (target / ".cipher" / "log").write_text("not a directory", encoding="utf-8")

            config = load_config(target)

            self.assertIsNone(config.compile_database_path)


if __name__ == "__main__":
    unittest.main()
