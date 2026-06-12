import os
import tempfile
import unittest
from pathlib import Path

from cipher2.config import ConfigError, load_config, normalize_compile_database_path, safe_cipher_path


class ConfigPathSafetyTest(unittest.TestCase):
    def test_safe_cipher_path_rejects_parent_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            with self.assertRaises(ConfigError) as caught:
                safe_cipher_path(target, "..", "outside")

            self.assertEqual(caught.exception.code, "path_escape")

    def test_safe_cipher_path_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            target = Path(tmp)
            (target / ".cipher").mkdir()
            os.symlink(Path(outside), target / ".cipher" / "snapshots")

            with self.assertRaises(ConfigError) as caught:
                safe_cipher_path(target, "snapshots", "current")

            self.assertEqual(caught.exception.code, "path_escape")

    def test_safe_cipher_path_rejects_cipher_directory_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            target = Path(tmp)
            os.symlink(Path(outside), target / ".cipher")

            with self.assertRaises(ConfigError) as caught:
                safe_cipher_path(target, "config.yml")

            self.assertEqual(caught.exception.code, "path_escape")

    def test_load_config_rejects_cipher_directory_symlink_escape_before_reading(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            target = Path(tmp)
            outside_path = Path(outside)
            (outside_path / "config.yml").write_text(
                "schema_version: 1\npaths:\n  compile_database:\n",
                encoding="utf-8",
            )
            os.symlink(outside_path, target / ".cipher")

            with self.assertRaises(ConfigError) as caught:
                load_config(target, observe=False)

            self.assertEqual(caught.exception.code, "path_escape")

    def test_compile_database_inside_cipher_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            path = target / ".cipher" / "compile_commands.json"
            path.parent.mkdir()
            path.write_text("[]", encoding="utf-8")

            with self.assertRaises(ConfigError) as caught:
                normalize_compile_database_path(target, ".cipher/compile_commands.json")

            self.assertEqual(caught.exception.code, "path_escape")

    def test_compile_database_unreadable_or_empty_path_is_rejected(self):
        cases = ["", "missing/compile_commands.json"]
        for value in cases:
            with self.subTest(value=value):
                with tempfile.TemporaryDirectory() as tmp:
                    target = Path(tmp)

                    with self.assertRaises(ConfigError) as caught:
                        normalize_compile_database_path(target, value)

                    self.assertEqual(caught.exception.code, "compile_database_unreadable")

    def test_non_string_compile_database_path_is_invalid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            with self.assertRaises(ConfigError) as caught:
                normalize_compile_database_path(target, 123)  # type: ignore[arg-type]

            self.assertEqual(caught.exception.code, "invalid_config")

    def test_posix_backslash_relative_path_is_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            compile_db = target / "build" / "compile_commands.json"
            compile_db.parent.mkdir()
            compile_db.write_text("[]", encoding="utf-8")

            path = normalize_compile_database_path(target, "build\\compile_commands.json")

            self.assertEqual(path, compile_db.resolve(strict=False))


if __name__ == "__main__":
    unittest.main()
