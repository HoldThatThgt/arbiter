import tempfile
import unittest
from pathlib import Path

from cipher2.mcp import open_mcp_server
from cipher2.storage import FactRecord, open_fact_store


def _fact(object_id: str, object_source: str):
    return FactRecord(
        object_id=object_id,
        object_name=object_id,
        object_description="path safety",
        object_source=object_source,
        object_profile="debug",
        payload={"fact_kind": "function"},
    )


class McpPathSafetyTest(unittest.TestCase):
    def test_unrecognized_source_format_is_warning_without_absolute_path_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact("fact:bad", "opaque provenance")])

            result = open_mcp_server(target).call_tool("detail", {"fact_id": "fact:bad", "budget": "normal"})

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["source_context"]["unavailable_reason"], "unrecognized_source_format")
        self.assertNotIn(str(target), result.content[0]["text"])

    def test_source_path_escape_is_rejected_without_reading_outside_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "repo"
            target.mkdir()
            secret = Path(tmp) / "secret.c"
            secret.write_text("secret token should not leak\n", encoding="utf-8")
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact("fact:escape", "../secret.c:1")])

            response = open_mcp_server(target).detail("fact:escape", budget="normal")

        self.assertEqual(response.source_context.unavailable_reason, "source_path_escape")
        self.assertEqual(response.source_context.lines, [])

    def test_source_unreadable_reports_warning_and_no_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact("fact:missing", "src/missing.c:99")])

            result = open_mcp_server(target).call_tool("detail", {"fact_id": "fact:missing", "budget": "normal"})

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["source_context"]["unavailable_reason"], "source_unreadable")
        self.assertNotIn("Traceback", result.content[0]["text"])


if __name__ == "__main__":
    unittest.main()
