import tempfile
import unittest
from pathlib import Path

from cipher2.config import ConfigError, load_config, write_default_config


class ConfigFileTest(unittest.TestCase):
    def test_write_default_config_uses_config_yml_schema_and_atomic_tmp_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            compile_db = target / "build" / "compile_commands.json"
            compile_db.parent.mkdir()
            compile_db.write_text("[]", encoding="utf-8")

            clang = target / "bin" / "clang"
            clang.parent.mkdir()
            clang.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            clang.chmod(0o755)
            gcc = target / "bin" / "gcc"
            gcc.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            gcc.chmod(0o755)

            config = write_default_config(
                target,
                compile_database="build/compile_commands.json",
                clang_executable="bin/clang",
                gcc_executable="bin/gcc",
                clang_args=["-Iinclude", "-DDEBUG=1"],
                observe=False,
            )

            self.assertEqual(config.compile_database_path, compile_db.resolve(strict=False))
            self.assertEqual(config.clang_executable, str(clang.resolve(strict=False)))
            self.assertEqual(config.gcc_executable, str(gcc.resolve(strict=False)))
            self.assertEqual(config.clang_args, ["-Iinclude", "-DDEBUG=1"])
            self.assertGreaterEqual(config.extractor_worker_count, 1)
            self.assertLessEqual(config.extractor_worker_count, 32)
            self.assertTrue(config.config_path.exists())
            self.assertFalse((target / ".cipher" / "config.yml.tmp").exists())
            text = config.config_path.read_text(encoding="utf-8")
            self.assertIn("# 配置 schema 版本", text)
            self.assertIn("# 可选 compile_commands.json 路径", text)
            self.assertIn("auto，支持范围 1..32", text)
            self.assertIn("  compile_database: build/compile_commands.json\n", text)
            self.assertIn("    clang_executable: bin/clang\n", text)
            self.assertIn("    gcc_executable: bin/gcc\n", text)
            self.assertIn("      - -Iinclude\n", text)
            self.assertIn("      - -DDEBUG=1\n", text)
            self.assertIn("# MCP/runtime views 使用的临时增量 overlay 设置", text)

    def test_load_existing_relative_compile_database_is_repo_relocatable(self):
        with tempfile.TemporaryDirectory() as old_tmp, tempfile.TemporaryDirectory() as new_tmp:
            old_target = Path(old_tmp)
            new_target = Path(new_tmp)
            for target in (old_target, new_target):
                compile_db = target / "build" / "compile_commands.json"
                compile_db.parent.mkdir()
                compile_db.write_text("[]", encoding="utf-8")
            (new_target / ".cipher").mkdir()
            (new_target / ".cipher" / "config.yml").write_text(
                "schema_version: 1\npaths:\n  compile_database: build/compile_commands.json\n",
                encoding="utf-8",
            )

            config = load_config(new_target, observe=False)

            self.assertEqual(config.compile_database_path, (new_target / "build" / "compile_commands.json").resolve(strict=False))
            self.assertGreaterEqual(config.extractor_worker_count, 1)
            self.assertLessEqual(config.extractor_worker_count, 32)

    def test_absolute_external_compile_database_is_allowed_and_revalidated(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as external:
            target = Path(tmp)
            compile_db = Path(external) / "compile_commands.json"
            compile_db.write_text("[]", encoding="utf-8")
            (target / ".cipher").mkdir()
            (target / ".cipher" / "config.yml").write_text(
                f"schema_version: 1\npaths:\n  compile_database: {compile_db}\n",
                encoding="utf-8",
            )

            config = load_config(target, observe=False)

            self.assertEqual(config.compile_database_path, compile_db.resolve(strict=False))

    def test_overrides_take_precedence_without_writing_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            compile_db = target / "build" / "compile_commands.json"
            compile_db.parent.mkdir()
            compile_db.write_text("[]", encoding="utf-8")

            config = load_config(
                target,
                overrides={"paths": {"compile_database": "build/compile_commands.json"}},
                observe=False,
            )

            self.assertEqual(config.compile_database_path, compile_db.resolve(strict=False))
            self.assertFalse((target / ".cipher" / "config.yml").exists())

    def test_load_existing_clang_executable_and_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            compile_db = target / "build" / "compile_commands.json"
            compile_db.parent.mkdir()
            compile_db.write_text("[]", encoding="utf-8")
            clang = target / "bin" / "clang"
            clang.parent.mkdir()
            clang.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            clang.chmod(0o755)
            gcc = target / "bin" / "gcc"
            gcc.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            gcc.chmod(0o755)
            (target / ".cipher").mkdir()
            (target / ".cipher" / "config.yml").write_text(
                "schema_version: 1\n"
                "paths:\n"
                "  compile_database: build/compile_commands.json\n"
                "extractor:\n"
                "  code:\n"
                "    clang_executable: bin/clang\n"
                "    libclang_library: lib/libclang.so\n"
                "    gcc_executable: bin/gcc\n"
                "    clang_args:\n"
                "      - -Iinclude\n"
                "      - -DNAME=1\n",
                encoding="utf-8",
            )

            libclang = target / "lib" / "libclang.so"
            libclang.parent.mkdir()
            libclang.write_text("fake", encoding="utf-8")

            config = load_config(target, observe=False)

            self.assertEqual(config.clang_executable, str(clang.resolve(strict=False)))
            self.assertEqual(config.libclang_library_path, libclang.resolve(strict=False))
            self.assertEqual(config.gcc_executable, str(gcc.resolve(strict=False)))
            self.assertEqual(config.clang_args, ["-Iinclude", "-DNAME=1"])
            self.assertGreaterEqual(config.extractor_worker_count, 1)
            self.assertLessEqual(config.extractor_worker_count, 32)

    def test_extractor_worker_count_loads_explicit_auto_and_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / ".cipher").mkdir()
            (target / ".cipher" / "config.yml").write_text(
                "schema_version: 1\n"
                "paths:\n"
                "  compile_database:\n"
                "extractor:\n"
                "  code:\n"
                "    clang_executable:\n"
                "    clang_args:\n"
                "  worker_count: 32\n",
                encoding="utf-8",
            )

            explicit = load_config(target, observe=False)
            overridden = load_config(target, overrides={"extractor": {"worker_count": 2}}, observe=False)

            self.assertEqual(explicit.extractor_worker_count, 32)
            self.assertEqual(overridden.extractor_worker_count, 2)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            config = write_default_config(target, extractor_worker_count=1, observe=False)

            self.assertEqual(config.extractor_worker_count, 1)
            self.assertIn("  worker_count: 1\n", config.config_path.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / ".cipher").mkdir()
            (target / ".cipher" / "config.yml").write_text(
                "schema_version: 1\npaths:\n  compile_database:\nextractor:\n  worker_count:\n",
                encoding="utf-8",
            )

            auto = load_config(target, observe=False)

            self.assertGreaterEqual(auto.extractor_worker_count, 1)
            self.assertLessEqual(auto.extractor_worker_count, 32)

    def test_invalid_extractor_worker_count_is_rejected(self):
        cases = [
            "extractor:\n  worker_count: 0\n",
            "extractor:\n  worker_count: 33\n",
            "extractor:\n  worker_count: true\n",
            "extractor:\n  worker_count: many\n",
        ]
        for fragment in cases:
            with self.subTest(fragment=fragment):
                with tempfile.TemporaryDirectory() as tmp:
                    target = Path(tmp)
                    (target / ".cipher").mkdir()
                    (target / ".cipher" / "config.yml").write_text(
                        "schema_version: 1\npaths:\n  compile_database:\n" + fragment,
                        encoding="utf-8",
                    )

                    with self.assertRaises(ConfigError) as caught:
                        load_config(target, observe=False)

                    self.assertEqual(caught.exception.code, "invalid_config")

    def test_invalid_clang_config_is_rejected(self):
        cases = [
            (
                "extractor:\n  code:\n    clang_executable: .cipher/clang\n    clang_args:\n",
                "path_escape",
            ),
            (
                "extractor:\n  code:\n    gcc_executable: .cipher/gcc\n    clang_args:\n",
                "path_escape",
            ),
            (
                "extractor:\n  code:\n    clang_executable:\n    clang_args:\n      - -o\n",
                "invalid_config",
            ),
            (
                "extractor:\n  code:\n    clang_executable:\n    clang_args:\n      - good\u0000bad\n",
                "invalid_config",
            ),
        ]
        for fragment, code in cases:
            with self.subTest(fragment=fragment):
                with tempfile.TemporaryDirectory() as tmp:
                    target = Path(tmp)
                    (target / ".cipher").mkdir()
                    (target / ".cipher" / "config.yml").write_text(
                        "schema_version: 1\npaths:\n  compile_database:\n" + fragment,
                        encoding="utf-8",
                    )

                    with self.assertRaises(ConfigError) as caught:
                        load_config(target, observe=False)

                    self.assertEqual(caught.exception.code, code)

    def test_libclang_library_path_is_last_resort_readable_path(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as external:
            target = Path(tmp)
            libclang = target / "toolchain" / "libclang.so"
            libclang.parent.mkdir()
            libclang.write_text("fake", encoding="utf-8")
            config = write_default_config(target, libclang_library="toolchain/libclang.so", observe=False)

            self.assertEqual(config.libclang_library_path, libclang.resolve(strict=False))
            self.assertIn("    libclang_library: toolchain/libclang.so\n", config.config_path.read_text(encoding="utf-8"))

            external_lib = Path(external) / "libclang.so"
            external_lib.write_text("fake", encoding="utf-8")
            config = load_config(
                target,
                overrides={"extractor": {"code": {"libclang_library": str(external_lib)}}},
                observe=False,
            )

            self.assertEqual(config.libclang_library_path, external_lib.resolve(strict=False))

    def test_invalid_libclang_library_path_is_rejected(self):
        cases = [
            ("extractor:\n  code:\n    libclang_library: .cipher/libclang.so\n", "path_escape"),
            ("extractor:\n  code:\n    libclang_library: missing/libclang.so\n", "libclang_unavailable"),
            ("extractor:\n  code:\n    libclang_library: true\n", "invalid_config"),
        ]
        for fragment, code in cases:
            with self.subTest(fragment=fragment):
                with tempfile.TemporaryDirectory() as tmp:
                    target = Path(tmp)
                    (target / ".cipher").mkdir()
                    (target / ".cipher" / "config.yml").write_text(
                        "schema_version: 1\npaths:\n  compile_database:\n" + fragment,
                        encoding="utf-8",
                    )

                    with self.assertRaises(ConfigError) as caught:
                        load_config(target, observe=False)

                    self.assertEqual(caught.exception.code, code)

    def test_invalid_schema_version_raises_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / ".cipher").mkdir()
            (target / ".cipher" / "config.yml").write_text(
                "schema_version: 2\npaths:\n  compile_database:\n",
                encoding="utf-8",
            )

            with self.assertRaises(ConfigError) as caught:
                load_config(target, observe=False)

            self.assertEqual(caught.exception.code, "unsupported_schema_version")

    def test_invalid_scalar_and_malformed_yaml_raise_invalid_config(self):
        cases = [
            "schema_version: 1\npaths: bad\n",
            "schema_version: 1\npaths:\n  compile_database: [\n",
            "schema_version: true\npaths:\n  compile_database:\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                with tempfile.TemporaryDirectory() as tmp:
                    target = Path(tmp)
                    (target / ".cipher").mkdir()
                    (target / ".cipher" / "config.yml").write_text(text, encoding="utf-8")

                    with self.assertRaises(ConfigError) as caught:
                        load_config(target, observe=False)

                    self.assertEqual(caught.exception.code, "invalid_config")


if __name__ == "__main__":
    unittest.main()
