import unittest

from arbiter_engine.facts import extract_cache


def unit(source, *, body="int main(void) { return 0; }\n", headers=None, flags=(), toolchain="clang-18"):
    return extract_cache.ExtractUnit(
        source=source,
        tu_content=body,
        include_closure=headers or {},
        flags=flags,
        toolchain_id=toolchain,
    )


class ExtractCacheKeyTest(unittest.TestCase):
    def test_profile_switch_strips_codegen_flags(self):
        plain = [
            unit("src/a.c", flags=("-Iinclude", "-O0", "-g")),
            unit("src/b.c", flags=("-Iinclude", "-O0", "-g")),
        ]
        optimized = [
            unit("src/a.c", flags=("-Iinclude", "-O2", "-g3", "-fprofile-generate")),
            unit("src/b.c", flags=("-Iinclude", "-O2", "-g3", "-fprofile-generate")),
        ]

        self.assertEqual(
            extract_cache.changed_sources(plain, optimized),
            (),
        )

    def test_sanitizer_flags_always_participate_in_key(self):
        plain = unit("src/a.c", flags=("-Iinclude",))
        asan = unit("src/a.c", flags=("-Iinclude", "-fsanitize=address"))

        self.assertNotEqual(
            extract_cache.key_for_unit(plain),
            extract_cache.key_for_unit(asan),
        )
        self.assertEqual(
            extract_cache.changed_sources([plain], [asan]),
            ("src/a.c",),
        )
        self.assertEqual(
            extract_cache.clean_semantic_flags(
                ("-Iinclude", "-O2", "-g3", "-fsanitize=address", "-fprofile-generate", "--coverage")
            ),
            ("-Iinclude", "-fsanitize=address"),
        )

    def test_feature_define_changes_only_units_with_that_semantic_input(self):
        before = [
            unit("src/uses_feature.c", headers={"include/config.h": "#define WITH_X 0\n"}),
            unit("src/plain.c", headers={"include/plain.h": "#define PLAIN 1\n"}),
        ]
        after = [
            unit(
                "src/uses_feature.c",
                headers={"include/config.h": "#define WITH_X 1\n"},
                flags=("-DWITH_X",),
            ),
            unit("src/plain.c", headers={"include/plain.h": "#define PLAIN 1\n"}),
        ]

        self.assertEqual(
            extract_cache.changed_sources(before, after),
            ("src/uses_feature.c",),
        )

    def test_key_flags_restores_codegen_flag_sensitivity(self):
        debug = unit("src/a.c", flags=("-Iinclude", "-O0"))
        optimized = unit("src/a.c", flags=("-Iinclude", "-O2"))

        self.assertEqual(
            extract_cache.key_for_unit(debug),
            extract_cache.key_for_unit(optimized),
        )
        self.assertNotEqual(
            extract_cache.key_for_unit(debug, key_flags=("-O0", "-O2")),
            extract_cache.key_for_unit(optimized, key_flags=("-O0", "-O2")),
        )

    def test_toolchain_id_and_tu_content_are_keyed(self):
        base = unit("src/a.c")

        self.assertNotEqual(base.key(), unit("src/a.c", body="int main(void) { return 1; }\n").key())
        self.assertNotEqual(base.key(), unit("src/a.c", toolchain="clang-19").key())


if __name__ == "__main__":
    unittest.main()
