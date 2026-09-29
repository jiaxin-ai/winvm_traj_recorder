"""Offline regression tests; these do not validate Windows COM delivery."""
import collections
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.autocad.adapter import LOG_ANSI_ENCODING, LOG_PATTERNS, LogParser, LogTail, _Sink


class LogTests(unittest.TestCase):
    def test_real_en_log_unknown_command_cp1252_quotes(self):
        # Actual AutoCAD output: escaped Chinese, but ANSI smart quotes.
        data = (b"\\U+547D\\U+4EE4: ZZZ_NONEEXISTENT\r\n"
                b"\\U+672A\\U+77E5\\U+547D\\U+4EE4\x93ZZZ_NONEEXISTENT\x94\\U+3002\r\n"
                b"\\U+547D\\U+4EE4: LINE\r\n"
                b"\\U+6307\\U+5B9A\\U+7B2C\\U+4E00\\U+4E2A\\U+70B9: *\\U+53D6\\U+6D88*\r\n")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.log"
            path.write_bytes(data)
            tail = LogTail(str(path), 0, LOG_ANSI_ENCODING["EN"])
            parser = LogParser(LOG_PATTERNS["EN"])
            results = []
            while tail.offset < len(data):
                lines, _ = tail.read_lines(max_bytes=7)
                results.extend(result for line in lines if (result := parser.parse(line)))
            self.assertEqual(results, [("command", "ZZZ_NONEEXISTENT"),
                                      ("unknown_command", "ZZZ_NONEEXISTENT"),
                                      ("command", "LINE"), ("cancel", "LINE")])

    def test_real_sample_prompt_and_cancel(self):
        parser = LogParser(LOG_PATTERNS["EN"])
        self.assertEqual(parser.parse(r"\U+547D\U+4EE4: LOGFILEMODE"),
                         ("command", "LOGFILEMODE"))
        self.assertEqual(parser.parse(r"\U+8F93\U+5165 LOGFILEMODE <1>: *\U+53D6\U+6D88*"),
                         ("cancel", "LOGFILEMODE"))
        self.assertEqual(parser.parse(r"\U+547D\U+4EE4: *\U+53D6\U+6D88*"),
                         ("cancel", None))

    def test_english_and_synthetic_errors(self):
        parser = LogParser(LOG_PATTERNS["EN"])
        self.assertEqual(parser.parse("Command: _LINE"), ("command", "LINE"))
        self.assertEqual(parser.parse("Command: *Cancel*"), ("cancel", "LINE"))
        self.assertEqual(parser.parse('Unknown command "ZZZ_NONEXISTENT".'),
                         ("unknown_command", "ZZZ_NONEXISTENT"))
        self.assertEqual(parser.parse('未知命令“ZZZ_NONEXISTENT”。'),
                         ("unknown_command", "ZZZ_NONEXISTENT"))

    def test_incremental_escaped_log(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.log"
            path.write_bytes("插件已加载\r\n".encode("gbk") +
                             b"\\U+547D\\U+4EE4: LINE\r\n")
            tail = LogTail(str(path), 0, "gbk")
            parser = LogParser(LOG_PATTERNS["EN"])
            results = []
            while tail.offset < path.stat().st_size:
                lines, _ = tail.read_lines(max_bytes=7)
                results.extend(result for line in lines if (result := parser.parse(line)))
            self.assertEqual(results, [("command", "LINE")])
            self.assertEqual(tail.read_lines()[0], [])


class SinkTests(unittest.TestCase):
    def test_custom_iid_returns_dispatch_wrapper_and_callback_enqueues(self):
        raw = collections.deque()
        sink = _Sink("event-iid", {1: "BeginCommand"}, {"BeginCommand"}, raw, None)
        sentinel = object()
        util = types.ModuleType("win32com.server.util")
        util.wrap = lambda obj: sentinel if obj is sink else None
        with patch.dict(sys.modules, {"win32com.server.util": util}):
            self.assertIs(sink._query_interface_("event-iid"), sentinel)
            self.assertIsNone(sink._query_interface_("unrelated-iid"))
        sink.OnBeginCommand("LINE")
        self.assertEqual(raw[0][1:], ("BeginCommand", ("LINE",), None))
        self.assertIsInstance(raw[0][0], int)


if __name__ == "__main__":
    unittest.main()
