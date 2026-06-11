import tempfile
import unittest
from pathlib import Path

from cipher2.mcp import open_mcp_server
from cipher2.storage import FactRecord, open_fact_store
from cipher2.tools.log import open_log
from cipher2.tools.views import build_overview


def _fact():
    return FactRecord(
        object_id="fact:obs",
        object_name="Observed",
        object_description="alpha observed",
        object_source="src/observed.c:1",
        object_profile="debug",
        payload={"fact_kind": "function", "rank": 1},
    )


class McpObservabilityTest(unittest.TestCase):
    def test_search_detail_and_errors_write_mcp_events_visible_in_log_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            source = target / "src" / "observed.c"
            source.parent.mkdir()
            source.write_text("int observed(void) { return 1; }\n", encoding="utf-8")
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])
            server = open_mcp_server(target)

            server.call_tool("search", {"query": "alpha", "limit": 1})
            server.call_tool("detail", {"fact_id": "fact:obs", "budget": "normal"})
            server.call_tool("search", {"query": "alpha", "limit": 0})
            events = open_log(target).read_events(channel="mcp").events
            overview = build_overview(target, include_sections=["log"], top_n=10)

        self.assertEqual(overview.log.events_by_channel["mcp"], 6)
        self.assertIn(("mcp.search", 1), overview.log.top_event_names)
        self.assertIn(("mcp.detail", 1), overview.log.top_event_names)
        self.assertEqual(overview.log.error_codes["invalid_limit"], 1)
        fields = [field for row in overview.log.recent_events for field in row.fields]
        self.assertIn(("tool_name", "search"), fields)
        self.assertIn(("request_kind", "tool_call"), fields)
        self.assertIn(("budget", "normal"), fields)
        self.assertIn(("count.result_count", "1"), fields)
        self.assertIn(("count.response_bytes_limit", "32768"), fields)
        self.assertIn(("count.flat_relative_count", "0"), fields)
        self.assertIn(("count.context_line_count", "1"), fields)
        search_event = next(event for event in events if event.event_name == "mcp.search")
        detail_event = next(event for event in events if event.event_name == "mcp.detail")
        self.assertEqual(search_event.schema_version, 2)
        self.assertEqual(search_event.payload["returned_ids"], ["fact:obs"])
        self.assertNotIn("query_sha256", search_event.payload)
        self.assertIn("base_snapshot_id", search_event.payload)
        self.assertEqual(detail_event.subject_id, "fact:obs")
        self.assertIn("base_snapshot_id", detail_event.payload)
        self.assertGreater(detail_event.counts["response_bytes"], 0)
        self.assertEqual(detail_event.counts["response_bytes_limit"], 32 * 1024)
        self.assertEqual(detail_event.counts["response_truncated_count"], 0)
        self.assertFalse(detail_event.payload["response_truncated"])

    def test_detail_source_warning_keeps_fact_subject_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])

            result = open_mcp_server(target).call_tool("detail", {"fact_id": "fact:obs", "budget": "normal"})
            detail_event = next(
                event for event in open_log(target).read_events(channel="mcp").events
                if event.event_name == "mcp.detail"
            )

        self.assertFalse(result.is_error)
        self.assertEqual(detail_event.status, "warning")
        self.assertEqual(detail_event.error_code, "source_unreadable")
        self.assertEqual(detail_event.subject_id, "fact:obs")

    def test_mcp_does_not_call_observe_batch_or_write_batch_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])

            open_mcp_server(target).call_tool("search", {"query": "", "limit": 1})

            event_names = open_log(target).summarize(channel="mcp").events_by_name
        self.assertNotIn("mcp.batch_summary", event_names)

    def test_log_disabled_suppresses_mcp_channel_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            open_fact_store(target, mode="w", log_enabled=False).replace_facts([_fact()])

            result = open_mcp_server(target, log_enabled=False).call_tool("search", {"query": "", "limit": 1})

            self.assertFalse(result.is_error)
            self.assertEqual(open_log(target).summarize(channel="mcp").total_events, 0)


if __name__ == "__main__":
    unittest.main()
