"""Offline regression tests; these do not validate Windows COM delivery."""
import collections
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.autocad.adapter import Adapter, LOG_ANSI_ENCODING, LOG_PATTERNS, LogParser, LogTail, _Sink, _Busy, _Dead


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


class InitializationTests(unittest.TestCase):
    def test_busy_document_enumeration_recovers_on_later_call(self):
        adapter = Adapter()
        adapter._app = object()
        def recovered_scan(initial=False):
            self.assertTrue(initial)
            adapter._rescan_needed = False
        with patch.object(adapter, "_rescan", side_effect=_Busy()), \
                patch.object(adapter, "_init_active_doc_caches") as caches:
            adapter._initialize_documents()
            caches.assert_not_called()
        self.assertIsNotNone(adapter._app)
        self.assertTrue(adapter._initial_documents_pending)
        self.assertTrue(adapter._rescan_needed)
        with patch.object(adapter, "_rescan", side_effect=recovered_scan), \
                patch.object(adapter, "_init_active_doc_caches") as caches, \
                patch.object(adapter, "_responsive", return_value=True):
            adapter._maintain(time.perf_counter() + 1)
            caches.assert_called_once()
        self.assertFalse(adapter._initial_documents_pending)
        self.assertFalse(adapter._rescan_needed)

    def test_busy_cache_read_rearms_retry_even_after_scan_clears_flag(self):
        adapter = Adapter()
        def scan(initial=False):
            adapter._rescan_needed = False
        with patch.object(adapter, "_rescan", side_effect=scan), \
                patch.object(adapter, "_init_active_doc_caches", side_effect=_Busy()):
            adapter._initialize_documents()
        self.assertTrue(adapter._initial_documents_pending)
        self.assertTrue(adapter._rescan_needed)

    def test_disconnect_or_unexpected_error_is_not_treated_as_busy(self):
        for error in (_Dead(), ValueError("bad data")):
            adapter = Adapter()
            with patch.object(adapter, "_rescan", side_effect=error):
                with self.assertRaises(type(error)):
                    adapter._initialize_documents()


class SinkTests(unittest.TestCase):
    def test_crash_quarantine_skips_subscription_for_both_apartment_modes(self):
        for mta in (True, False):
            adapter = Adapter()
            with patch.object(adapter, "_setup_events") as setup:
                adapter._configure_events(mta)
                setup.assert_not_called()
            self.assertFalse(adapter._events_on)
            self.assertIn("AC-005", adapter._events_off_reason)
            # Reject even an accidental direct subscription attempt.
            with self.assertRaisesRegex(RuntimeError, "AC-005"):
                adapter._advise(None, None, None, None)

    def test_log_actions_and_errors_remain_available_without_com_events(self):
        adapter = Adapter()
        adapter._configure_events(True)
        parser = LogParser(LOG_PATTERNS["EN"])
        for line in ("命令: LINE", "指定第一个点: *取消*",
                     '未知命令“ZZZ_NONEEXISTENT”。'):
            parsed = parser.parse(line)
            if parsed:
                adapter._on_log_line(123, parsed, parser, line)
        actions = adapter._actions.take()
        events = adapter._events.take()
        self.assertEqual([(a["name"], a["source"]) for a in actions], [("LINE", "log")])
        self.assertEqual([e["type"] for e in events], ["command_cancelled", "error_raised"])

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
