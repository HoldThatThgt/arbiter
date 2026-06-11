import io
import tempfile
import unittest
from pathlib import Path

from cipher2.cli import CliArgs, StatusCliArgs, main, parse_args


class CliParserTest(unittest.TestCase):
    def test_parse_init_defaults_and_repeated_source_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            args = parse_args(
                [
                    "init",
                    str(target),
                    "--source-root",
                    "src",
                    "--source-root",
                    "include",
                    "--profile",
                    "debug",
                    "--json",
                ]
            )

        self.assertIsInstance(args, CliArgs)
        self.assertEqual(args.command, "init")
        self.assertEqual(args.target_repo, target)
        self.assertEqual(args.source_roots, [Path("src"), Path("include")])
        self.assertEqual(args.profile, "debug")
        self.assertIsNone(args.compile_database)
        self.assertFalse(args.no_mcp_config)
        self.assertFalse(args.print_mcp_config)
        self.assertTrue(args.log_enabled)
        self.assertTrue(args.progress_enabled)
        self.assertTrue(args.json_output)

    def test_parse_no_log_and_compile_database_are_single_invocation_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            args = parse_args(
                [
                    "init",
                    str(target),
                    "--compile-database",
                    "build/compile_commands.json",
                    "--no-log",
                ]
            )

        self.assertEqual(args.command, "init")
        self.assertEqual(args.compile_database, Path("build/compile_commands.json"))
        self.assertFalse(args.no_mcp_config)
        self.assertFalse(args.print_mcp_config)
        self.assertFalse(args.log_enabled)
        self.assertTrue(args.progress_enabled)
        self.assertFalse(args.json_output)

    def test_parse_init_setup_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            args = parse_args(["init", str(target), "--no-mcp-config", "--print-mcp-config"])

        self.assertIsInstance(args, CliArgs)
        self.assertTrue(args.no_mcp_config)
        self.assertTrue(args.print_mcp_config)

    def test_external_client_and_toolchain_write_flags_are_not_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            for flag in ("--client", "--write-toolchain-config"):
                with self.subTest(flag=flag):
                    stdout = io.StringIO()
                    stderr = io.StringIO()

                    exit_code = main(["init", str(target), flag], stdout=stdout, stderr=stderr)

                    self.assertEqual(exit_code, 2)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertIn("unrecognized arguments", stderr.getvalue())
                    self.assertNotIn("Traceback", stderr.getvalue())

    def test_parse_no_progress_only_affects_init_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            args = parse_args(["init", str(target), "--no-progress"])

        self.assertIsInstance(args, CliArgs)
        self.assertFalse(args.progress_enabled)

    def test_interactive_option_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = main(["init", str(target), "--interactive"], stdout=stdout, stderr=stderr)

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("unrecognized arguments: --interactive", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_parse_rebuild_uses_same_options_with_rebuild_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            args = parse_args(["rebuild", str(target), "--source-root", "src", "--json"])

        self.assertEqual(args.command, "rebuild")
        self.assertEqual(args.target_repo, target)
        self.assertEqual(args.source_roots, [Path("src")])
        self.assertFalse(args.progress_enabled)
        self.assertTrue(args.json_output)

    def test_parse_status_only_accepts_target_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            args = parse_args(["status", str(target), "--json"])

        self.assertIsInstance(args, StatusCliArgs)
        self.assertEqual(args.command, "status")
        self.assertEqual(args.target_repo, target)
        self.assertTrue(args.json_output)

    def test_usage_errors_exit_two_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = main(["missing-command", str(target)], stdout=stdout, stderr=stderr)

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        stderr_text = stderr.getvalue()
        self.assertIn("usage:", stderr_text)
        self.assertNotIn("Traceback", stderr_text)

    def test_status_usage_errors_exit_two_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            cases = [
                ["status"],
                ["status", str(target), "--source-root", "src"],
            ]
            for argv in cases:
                with self.subTest(argv=argv):
                    stdout = io.StringIO()
                    stderr = io.StringIO()

                    exit_code = main(argv, stdout=stdout, stderr=stderr)

                    self.assertEqual(exit_code, 2)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertIn("usage:", stderr.getvalue())
                    self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
