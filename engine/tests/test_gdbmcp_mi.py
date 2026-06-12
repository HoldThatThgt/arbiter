import unittest

from arbiter_engine.gdbmcp import mi


class MITest(unittest.TestCase):
    def test_parse_nested_result_record(self):
        record = mi.parse_line('12^done,stack=[frame={level="0",func="main",line="3"}],value="a\\nb"')
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.token, 12)
        self.assertEqual(record.kind, "result")
        self.assertEqual(record.cls, "done")
        self.assertEqual(record.results["value"], "a\nb")
        self.assertEqual(record.results["stack"][0]["frame"]["func"], "main")

    def test_parse_stream_record(self):
        record = mi.parse_line('~"hello\\n"')
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.kind, "console")
        self.assertEqual(record.text, "hello\n")

    def test_quote_escapes(self):
        self.assertEqual(mi.quote('a"b\\c'), '"a\\"b\\\\c"')


if __name__ == "__main__":
    unittest.main()

