import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from arbiter_engine import rpc


def request(method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message, separators=(",", ":")) + "\n"


def tool_call(name, arguments, request_id=1):
    return request("tools/call", {"name": name, "arguments": arguments}, request_id=request_id)


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


class RunToolsTest(unittest.TestCase):
    def test_run_tool_executes_registered_gtest_recipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_fake_gtest(root / "fake_gtest.sh", failed=False)
            self.write_recipe(root)

            response = response_for(
                tool_call(
                    "run",
                    {
                        "recipe": "unit",
                        "tests": ["Suite.Pass"],
                        "options": {"harness_options": {"gtest": {"fail_fast": False}}},
                    },
                ),
                root,
            )

            result = response["result"]
            self.assertFalse(result["isError"])
            self.assertEqual(result["overall"], "passed")
            self.assertEqual(result["passed"], 1)
            self.assertEqual(result["per_test"][0]["name"], "Pass")
            self.assertIn("--gtest_filter=Suite.Pass", (root / "args.log").read_text(encoding="utf-8"))

    def test_recipe_search_and_register_are_real_handlers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_fake_gtest(root / "fake_gtest.sh", failed=False)
            recipe = self.write_recipe(root)

            registered = response_for(tool_call("register", {"path": str(recipe)}), root)
            found = response_for(tool_call("recipe_search", {"query": "unit"}), root)

            self.assertEqual(registered["result"]["targets"], ["unit"])
            self.assertEqual(found["result"]["matches"][0]["id"], "unit")

    def test_run_options_are_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_fake_gtest(root / "fake_gtest.sh", failed=False)
            self.write_recipe(root)

            response = response_for(
                tool_call("run", {"recipe": "unit", "options": {"surprise": True}}),
                root,
            )

            self.assertEqual(response["error"]["data"]["kind"], "invalid_args")
            self.assertEqual(response["error"]["data"]["bad_options"], ["surprise"])

    def write_recipe(self, root):
        recipe = root / ".arbiter" / "recipes.yaml"
        recipe.parent.mkdir(parents=True)
        recipe.write_text(
            f"""
targets:
  - id: unit
    binary: fake_gtest.sh
    notes: fake gtest unit target
    harness:
      kind: gtest
    test_run:
      cmd: [{str(root / "fake_gtest.sh")}]
""",
            encoding="utf-8",
        )
        return recipe

    def write_fake_gtest(self, path, failed):
        body = (
            "#!/bin/sh\n"
            "printf '%s\\n' \"$@\" > args.log\n"
            "for arg in \"$@\"; do\n"
            "  case \"$arg\" in --gtest_output=xml:*) out=\"${arg#--gtest_output=xml:}\" ;; esac\n"
            "done\n"
            "mkdir -p \"$(dirname \"$out\")\"\n"
        )
        if failed:
            body += (
                "cat > \"$out\" <<'XML'\n"
                "<testsuites tests=\"1\" failures=\"1\"><testsuite name=\"Suite\"><testcase classname=\"Suite\" name=\"Fail\"><failure message=\"bad\"/></testcase></testsuite></testsuites>\n"
                "XML\nexit 1\n"
            )
        else:
            body += (
                "cat > \"$out\" <<'XML'\n"
                "<testsuites tests=\"1\" failures=\"0\"><testsuite name=\"Suite\"><testcase classname=\"Suite\" name=\"Pass\" time=\"0.001\"/></testsuite></testsuites>\n"
                "XML\nexit 0\n"
            )
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
