import io
import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from arbiter_engine import config
from arbiter_engine import rpc
from arbiter_engine.facts import descriptors
from arbiter_engine.facts import relocation


def response_for(line):
    stdin = io.StringIO(line)
    stdout = io.StringIO()
    rpc.serve(stdin, stdout)
    return json.loads(stdout.getvalue())


def request(method, params=None):
    message = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message, separators=(",", ":")) + "\n"


class FactsRelocationTest(unittest.TestCase):
    def test_facts_state_is_under_arbiter_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            self.assertEqual(relocation.facts_dir(root), root / ".arbiter" / "facts")
            self.assertEqual(relocation.config_path(root), root / ".arbiter" / "config.yml")

    def test_facts_config_is_nested_under_arbiter_config(self):
        parsed = config.parse_config(
            textwrap.dedent(
                """\
                facts:
                  extractor: clang
                  incremental:
                    enabled: true
                  index_on_build:
                    pool: 2
                    key_flags: [-DWITH_X]
                """
            )
        )

        self.assertEqual(parsed.facts.extractor, "clang")
        self.assertTrue(parsed.facts.incremental.enabled)
        self.assertEqual(parsed.facts.index_on_build.pool, 2)
        self.assertEqual(parsed.facts.index_on_build.key_flags, ("-DWITH_X",))

    def test_extractor_toolchain_overrides_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".arbiter").mkdir()
            (root / ".arbiter" / "config.yml").write_text(
                textwrap.dedent(
                    """\
                    facts:
                      toolchain:
                        clang: /usr/lib/llvm-16/bin/clang
                        libclang: /usr/lib/llvm-16/lib/libclang.so
                        clang_args: [--gcc-toolchain=/opt/gcc-7.3.0]
                    """
                ),
                encoding="utf-8",
            )

            overrides = relocation.extractor_toolchain_overrides(root)

        self.assertEqual(
            overrides,
            {
                "clang_executable": "/usr/lib/llvm-16/bin/clang",
                "libclang_library_path": Path("/usr/lib/llvm-16/lib/libclang.so"),
                "clang_args": ("--gcc-toolchain=/opt/gcc-7.3.0",),
            },
        )

    def test_extractor_toolchain_overrides_omits_unset_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".arbiter").mkdir()
            (root / ".arbiter" / "config.yml").write_text(
                "facts:\n  toolchain:\n    clang: /opt/clang\n", encoding="utf-8"
            )

            overrides = relocation.extractor_toolchain_overrides(root)

        # Unset libclang/clang_args stay absent so the extractor keeps auto-detecting them.
        self.assertEqual(overrides, {"clang_executable": "/opt/clang"})

    def test_extractor_toolchain_overrides_fail_soft(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # No .arbiter/config.yml at all -> no overrides (full auto-detect).
            self.assertEqual(relocation.extractor_toolchain_overrides(root), {})

            (root / ".arbiter").mkdir()
            (root / ".arbiter" / "config.yml").write_text(
                "facts:\n  toolchain:\n    bogus: 1\n", encoding="utf-8"
            )
            # A malformed config must not crash the indexer; it degrades to auto-detect.
            self.assertEqual(relocation.extractor_toolchain_overrides(root), {})

    def test_descriptors_keep_cipher_search_detail_inputs_without_meta(self):
        tools = descriptors.tool_descriptors()

        self.assertEqual([tool["name"] for tool in tools], ["search", "detail"])
        search = tools[0]
        detail = tools[1]
        self.assertNotIn("_meta", search["inputSchema"]["properties"])
        self.assertNotIn("_meta", detail["inputSchema"]["properties"])
        self.assertEqual(search["inputSchema"]["required"], ["query"])
        self.assertEqual(
            search["inputSchema"]["properties"]["limit"],
            {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        )
        self.assertNotIn("budget", search["inputSchema"]["properties"])
        self.assertEqual(detail["inputSchema"]["required"], ["fact_id"])
        self.assertEqual(
            detail["inputSchema"]["properties"]["budget"],
            {"type": "string", "enum": ["small", "normal", "large"], "default": "normal"},
        )
        self.assertNotIn("id", detail["inputSchema"]["properties"])
        self.assertIn("outputSchema", search)
        self.assertIn("outputSchema", detail)

    def test_chassis_hosts_facts_without_meta_in_tool_schema(self):
        response = response_for(request("tools/list"))
        tools = {tool["name"]: tool for tool in response["result"]["tools"]}

        self.assertEqual(tools["search"]["inputSchema"], descriptors.search_descriptor()["inputSchema"])
        self.assertEqual(tools["detail"]["inputSchema"], descriptors.detail_descriptor()["inputSchema"])
        self.assertNotIn("_meta", tools["search"]["inputSchema"]["properties"])
        self.assertNotIn("_meta", tools["detail"]["inputSchema"]["properties"])

        # facts 工具以 cwd 为仓根(席位约定):沙箱化 cwd,杜绝 .arbiter/ 残留进源码树。
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.getcwd()
            os.chdir(tmp)
            try:
                detail = response_for(
                    request("tools/call", {"name": "detail", "arguments": {"fact_id": "fact:1"}})
                )
                old_arg = response_for(request("tools/call", {"name": "detail", "arguments": {"id": "fact:1"}}))
                search_limit = response_for(
                    request("tools/call", {"name": "search", "arguments": {"query": "callers:main", "limit": 50}})
                )
            finally:
                os.chdir(previous)

        self.assertTrue(detail["result"]["isError"])
        self.assertEqual(detail["result"]["structuredContent"]["error"]["code"], "not_found")
        self.assertEqual(old_arg["error"]["data"]["kind"], "invalid_args")
        self.assertFalse(search_limit["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
