import unittest
from pathlib import Path

from transcript_replay import load_transcript, transcript_paths


class TranscriptCorpusCoverageTest(unittest.TestCase):
    def test_corpus_covers_m2_chassis_surface(self):
        repo = Path(__file__).resolve().parents[2]
        entries = []
        stems = set()
        for path in transcript_paths(repo):
            stems.add(path.stem)
            entries.extend(load_transcript(path))

        self.assertTrue(entries, "expected transcript corpus")
        self.assertIn("initialize", stems)
        self.assertIn("tools_list", stems)

        methods = set()
        tools = set()
        budgets = set()
        limits = set()
        error_kinds = set()
        for entry in entries:
            message = entry.get("message", {})
            if entry.get("type") == "request":
                method = message.get("method")
                methods.add(method)
                if method == "tools/call":
                    params = message.get("params", {})
                    name = params.get("name")
                    tools.add(name)
                    budget = params.get("arguments", {}).get("budget")
                    if budget:
                        budgets.add((name, budget))
                    limit = params.get("arguments", {}).get("limit")
                    if limit:
                        limits.add((name, limit))
            elif entry.get("type") == "response" and "error" in message:
                error_kinds.add(message["error"]["data"]["kind"])

        self.assertGreaterEqual(
            methods,
            {
                "initialize",
                "tools/list",
                "tools/call",
                "arbiter/handshake",
                "arbiter/refresh",
                "arbiter/census",
                "arbiter/resolveBriefing",
                "arbiter/startRun",
                "arbiter/runStatus",
                "arbiter/nope",
            },
        )
        self.assertGreaterEqual(
            tools,
            {"search", "detail", "run", "recipe_search", "register", "import_recipes", "scan"},
        )
        self.assertGreaterEqual(
            budgets,
            {
                ("detail", "small"),
                ("detail", "normal"),
                ("detail", "large"),
            },
        )
        self.assertGreaterEqual(
            limits,
            {
                ("search", 1),
                ("search", 20),
                ("search", 50),
            },
        )
        self.assertGreaterEqual(
            error_kinds,
            {"method_not_found", "invalid_args", "invalid_params", "engine_stale"},
        )


if __name__ == "__main__":
    unittest.main()
