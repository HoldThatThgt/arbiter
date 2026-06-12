import io
import json
import tempfile
import os
import unittest

from arbiter_engine import __version__
from arbiter_engine import rpc


def request(method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message, separators=(",", ":")) + "\n"


def response_for(line, router=None):
    stdin = io.StringIO(line)
    stdout = io.StringIO()
    if router is None:
        rpc.serve(stdin, stdout)
    else:
        rpc.serve(stdin, stdout, router=router)
    return json.loads(stdout.getvalue())


class RPCChassisTest(unittest.TestCase):
    def test_initialize_advertises_engine_and_tools(self):
        response = response_for(request("initialize", {"client": "test"}))

        self.assertEqual(
            response,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "engine": "arbiter-engine",
                    "version": __version__,
                    "capabilities": {"tools": True},
                },
            },
        )

    def test_tools_list_is_deterministic_and_namespaced(self):
        response = response_for(request("tools/list"))

        tools = response["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], sorted(tool["name"] for tool in tools))
        self.assertEqual(
            [tool["name"] for tool in tools],
            ["detail", "import_recipes", "recipe_search", "register", "run", "scan", "search"],
        )
        for tool in tools:
            if tool["name"] in {"search", "detail"}:
                self.assertEqual(sorted(tool), ["description", "inputSchema", "name", "outputSchema", "title"])
            else:
                self.assertEqual(sorted(tool), ["description", "inputSchema", "name"])
            self.assertFalse(tool["inputSchema"].get("additionalProperties", True))

    def test_tools_call_routes_to_registered_tool(self):
        # search 经默认路由触达 facts 视图,而引擎以 cwd 为仓根(席位约定):
        # 必须沙箱化 cwd,否则测试会把 .arbiter/ 残留写进源码树。
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.getcwd()
            os.chdir(tmp)
            try:
                response = response_for(
                    request("tools/call", {"name": "search", "arguments": {"query": "callers:main"}})
                )
            finally:
                os.chdir(previous)

        self.assertEqual(response["id"], 1)
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["query"], "callers:main")
        self.assertEqual(response["result"]["structuredContent"]["query_kind"], "relation")

    def test_meta_is_context_not_tool_argument(self):
        seen = {}

        def handler(context, arguments):
            seen["meta"] = context.meta
            seen["arguments"] = dict(arguments)
            return {"ok": True}

        router = rpc.Router()
        router.register(
            rpc.Tool(
                namespace="test",
                name="probe",
                description="test probe",
                input_schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                handler=handler,
            )
        )

        response = response_for(
            request(
                "tools/call",
                {
                    "name": "probe",
                    "arguments": {"value": "x"},
                    "_meta": {"match_id": "m1"},
                },
            ),
            router=router,
        )

        self.assertEqual(response["result"], {"ok": True})
        self.assertEqual(seen["meta"], {"match_id": "m1"})
        self.assertEqual(seen["arguments"], {"value": "x"})

    def test_unknown_arguments_are_rejected_by_closed_schema(self):
        response = response_for(
            request("tools/call", {"name": "search", "arguments": {"query": "q", "extra": True}})
        )

        self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(response["error"]["data"]["kind"], "invalid_args")
        self.assertEqual(response["error"]["data"]["bad_args"], ["extra"])

    def test_invalid_json_unknown_method_and_oversize_are_typed_errors(self):
        invalid = response_for("{not json}\n")
        self.assertEqual(invalid["error"]["data"]["kind"], "invalid_json")

        unknown = response_for(request("arbiter/nope"))
        self.assertEqual(unknown["error"]["code"], -32601)
        self.assertEqual(unknown["error"]["data"]["kind"], "method_not_found")

        oversized = response_for(" " * (rpc.MAX_LINE_BYTES + 1) + "\n")
        self.assertEqual(oversized["error"]["code"], -32600)
        self.assertEqual(oversized["error"]["data"]["kind"], "line_too_large")

    def test_unexpected_handler_exception_maps_to_internal_error_and_loop_survives(self):
        def explode(context, arguments):
            raise KeyError("missing-thing")

        router = rpc.Router()
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        router.register(rpc.Tool("test", "explode", "explode", schema, explode))
        router.register(rpc.Tool("test", "ok", "ok", schema, lambda c, a: {"ok": True}))

        stdin = io.StringIO(
            request("tools/call", {"name": "explode", "arguments": {}}, request_id=1)
            + request("tools/call", {"name": "ok", "arguments": {}}, request_id=2)
        )
        stdout = io.StringIO()
        rpc.serve(stdin, stdout, router=router)

        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        failed = json.loads(lines[0])
        self.assertEqual(failed["id"], 1)
        self.assertEqual(failed["error"]["code"], -32603)
        self.assertEqual(failed["error"]["data"]["kind"], "internal_error")
        self.assertEqual(failed["error"]["data"]["exception"], "KeyError")
        self.assertIn("missing-thing", failed["error"]["data"]["detail"])
        survived = json.loads(lines[1])
        self.assertEqual(survived["id"], 2)
        self.assertEqual(survived["result"], {"ok": True})

    def test_notifications_receive_no_response_even_on_error(self):
        notification_ok = json.dumps(
            {"jsonrpc": "2.0", "method": "tools/list"}, separators=(",", ":")
        )
        notification_error = json.dumps(
            {"jsonrpc": "2.0", "method": "arbiter/nope"}, separators=(",", ":")
        )
        stdin = io.StringIO(
            notification_ok + "\n" + notification_error + "\n" + request("initialize", request_id=7)
        )
        stdout = io.StringIO()
        rpc.serve(stdin, stdout)

        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["id"], 7)

    def test_descriptor_includes_title_only_when_set(self):
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        plain = rpc.Tool("test", "plain", "plain tool", schema, lambda c, a: {})
        titled = rpc.Tool("test", "titled", "titled tool", schema, lambda c, a: {}, title="Titled")

        self.assertNotIn("title", plain.descriptor())
        self.assertEqual(titled.descriptor()["title"], "Titled")
        self.assertEqual(
            sorted(titled.descriptor()), ["description", "inputSchema", "name", "title"]
        )

    def test_requests_are_processed_one_at_a_time(self):
        events = []

        def first(context, arguments):
            events.append(("first", len(events)))
            return {"ok": "first"}

        def second(context, arguments):
            events.append(("second", len(events)))
            return {"ok": "second"}

        router = rpc.Router()
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        router.register(rpc.Tool("test", "first", "first", schema, first))
        router.register(rpc.Tool("test", "second", "second", schema, second))

        stdin = io.StringIO(
            request("tools/call", {"name": "first", "arguments": {}}, request_id=1)
            + request("tools/call", {"name": "second", "arguments": {}}, request_id=2)
        )
        stdout = io.StringIO()
        rpc.serve(stdin, stdout, router=router)

        self.assertEqual(events, [("first", 0), ("second", 1)])


if __name__ == "__main__":
    unittest.main()
