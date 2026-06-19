import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from arbiter_engine import rpc
from arbiter_engine.facts.store import FactRecord, open_fact_store
from arbiter_engine.runs import discovery
from arbiter_engine.runs import state as run_state


def _test_body_fact(suite, name, file, line, fact_id, *, kind="type"):
    return FactRecord(
        object_id=fact_id,
        object_name=f"{suite}_{name}_Test",
        object_description=f"gtest fixture {suite}.{name}",
        object_source=f"{file}:{line}",
        object_profile="debug",
        payload={"fact_kind": kind},
    )


def _publish(root, facts):
    open_fact_store(root, mode="w", log_enabled=False).replace_snapshot(facts, [], [])


def tool_call(name, arguments, request_id=1):
    message = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    return json.dumps(message, separators=(",", ":")) + "\n"


def response_for(line, cwd):
    old = os.getcwd()
    try:
        os.chdir(cwd)
        stdin = io.StringIO(line)
        stdout = io.StringIO()
        rpc.serve(stdin, stdout)
        return json.loads(stdout.getvalue())
    finally:
        os.chdir(old)


class DiscoveryTest(unittest.TestCase):
    def test_discovers_testbody_function_facts_from_read_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(
                root,
                [
                    _test_body_fact("Lock", "Deadlock", "src/lock.cc", 7, "code:function:def"),
                    _test_body_fact("Suite", "Fail", "src/fail.cc", 42, "code:function:abc"),
                    # A plain function fact is not a test body and must be excluded.
                    FactRecord(
                        object_id="code:function:helper",
                        object_name="helper",
                        object_description="not a test",
                        object_source="src/h.c:3",
                        object_profile="debug",
                        payload={"fact_kind": "function"},
                    ),
                ],
            )

            candidates = discovery.discover_test_candidates(root)

            self.assertEqual(
                [(c.suite, c.name, c.file, c.line, c.fact_id) for c in candidates],
                [
                    ("Lock", "Deadlock", "src/lock.cc", 7, "code:function:def"),
                    ("Suite", "Fail", "src/fail.cc", 42, "code:function:abc"),
                ],
            )
            self.assertEqual([c.test for c in candidates], ["Lock.Deadlock", "Suite.Fail"])

    def test_payload_metadata_is_authoritative_over_name_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fact = FactRecord(
                object_id="code:function:param",
                object_name="My_Param_Suite_Some_Case_Test",
                object_description="parameterized fixture",
                object_source="src/p.cc:11",
                object_profile="debug",
                payload={
                    "fact_kind": "type",
                    "test_suite": "My_Param_Suite",
                    "test_name": "Some_Case",
                },
            )
            _publish(root, [fact])

            candidates = discovery.discover_test_candidates(root)

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].suite, "My_Param_Suite")
            self.assertEqual(candidates[0].name, "Some_Case")
            self.assertEqual(candidates[0].test, "My_Param_Suite.Some_Case")

    def test_scan_round_trips_through_scanned_test_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(
                root,
                [_test_body_fact("Suite", "Fail", "src/fail.cc", 42, "code:function:abc")],
            )

            result = discovery.scan(root, "tests")

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].suite, "Suite")
            self.assertEqual(result[0].name, "Fail")
            self.assertEqual(result[0].file, "src/fail.cc")
            self.assertEqual(result[0].line, 42)

            # The (previously dead) scanned_test table is now live state.
            db_path = root / ".arbiter" / "runs" / "state.sqlite"
            persisted = run_state.read_scanned_tests(db_path, discovery.scan_target_id("tests"))
            self.assertEqual(len(persisted), 1)
            self.assertEqual(persisted[0].target_id, "scan:tests")
            self.assertEqual(persisted[0].suite, "Suite")
            self.assertEqual(persisted[0].name, "Fail")
            self.assertEqual(persisted[0].file, "src/fail.cc")
            self.assertEqual(persisted[0].line, 42)

    def test_rescan_replaces_prior_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(root, [_test_body_fact("A", "One", "a.cc", 1, "code:function:1")])
            discovery.scan(root, "tests")

            _publish(root, [_test_body_fact("B", "Two", "b.cc", 2, "code:function:2")])
            result = discovery.scan(root, "tests")

            self.assertEqual([(c.suite, c.name) for c in result], [("B", "Two")])
            db_path = root / ".arbiter" / "runs" / "state.sqlite"
            persisted = run_state.read_scanned_tests(db_path, discovery.scan_target_id("tests"))
            self.assertEqual([(c.suite, c.name) for c in persisted], [("B", "Two")])

    def test_scan_is_fail_closed_without_a_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            self.assertEqual(discovery.discover_test_candidates(root), ())
            self.assertEqual(discovery.scan(root, "tests"), ())


class ScanToolTest(unittest.TestCase):
    def test_scan_tool_returns_facts_derived_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _publish(
                root,
                [_test_body_fact("Suite", "Fail", "src/fail.cc", 42, "code:function:abc")],
            )

            response = response_for(tool_call("scan", {"scope": "tests"}), root)

            result = response["result"]
            self.assertFalse(result["isError"])
            self.assertNotIn("stub", json.dumps(result))
            self.assertEqual(result["structuredContent"]["scope"], "tests")
            self.assertEqual(len(result["structuredContent"]["targets"]), 1)
            target = result["structuredContent"]["targets"][0]
            self.assertEqual(target["test"], "Suite.Fail")
            self.assertEqual(target["file"], "src/fail.cc")
            self.assertEqual(target["line"], 42)
            self.assertEqual(target["fact_id"], "code:function:abc")
            self.assertTrue(target["built"])
            self.assertEqual(result["content"][0]["text"], "1 test candidates (1 built)")

    def test_scan_tool_is_fail_closed_without_a_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            response = response_for(tool_call("scan", {"scope": "tests"}), root)

            result = response["result"]
            self.assertFalse(result["isError"])
            self.assertEqual(result["structuredContent"]["targets"], [])
            self.assertNotIn("stub", json.dumps(result))

    def test_scan_tool_rejects_non_string_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            response = response_for(tool_call("scan", {"scope": 7}), root)

            self.assertEqual(response["error"]["data"]["kind"], "invalid_args")
            self.assertEqual(response["error"]["data"]["field"], "scope")


if __name__ == "__main__":
    unittest.main()
