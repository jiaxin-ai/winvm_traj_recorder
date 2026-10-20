"""Host-side checks of the 2026-10-20 recorder fixes (no Windows needed):

    python -m unittest test_recorder_fixes
"""
import json
import shutil
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path

import input_recorder
import software
import trajectory


class FakeKeyCode:
    def __init__(self, char=None, vk=None):
        self.char, self.vk = char, vk


class KeyNames(unittest.TestCase):
    def setUp(self):
        self._saved = getattr(input_recorder, "keyboard", None)
        input_recorder.keyboard = types.SimpleNamespace(KeyCode=FakeKeyCode)

    def tearDown(self):
        if self._saved is None:
            del input_recorder.keyboard
        else:
            input_recorder.keyboard = self._saved

    def test_ctrl_combinations_are_named_by_their_key(self):
        name = input_recorder.key_to_name
        self.assertEqual(name(FakeKeyCode("\x16", 0x56)), "v")   # Ctrl+V
        self.assertEqual(name(FakeKeyCode("\x13", None)), "s")   # Ctrl+S without a virtual-key code
        self.assertEqual(name(FakeKeyCode(None, 0x31)), "1")
        self.assertEqual(name(FakeKeyCode("A", 0x41)), "a")
        self.assertEqual(name(FakeKeyCode("中", None)), "中")
        self.assertEqual(name(FakeKeyCode(None, 0xDB)), "vk219")


class InjectedFlag(unittest.TestCase):
    def test_events_carry_the_injected_flag_when_known(self):
        written = []
        recorder = input_recorder.InputRecorder(types.SimpleNamespace(write=written.append))
        recorder._emit({"type": "key_down", "key": "v"}, True)
        recorder._emit({"type": "mouse_down", "button": "left", "x": 1, "y": 2}, False)
        recorder._emit({"type": "key_down", "key": "a"})
        self.assertIs(written[0]["injected"], True)
        self.assertIs(written[1]["injected"], False)
        self.assertNotIn("injected", written[2])

    def test_hook_filters_read_the_windows_flags(self):
        recorder = input_recorder.InputRecorder(types.SimpleNamespace(write=lambda event: None))
        recorder._kb_filter(input_recorder._WM_KEYDOWN, types.SimpleNamespace(vkCode=0x41, flags=0x10))
        self.assertIs(recorder._key_injected, True)
        recorder._kb_filter(input_recorder._WM_KEYDOWN, types.SimpleNamespace(vkCode=0x41, flags=0x00))
        self.assertIs(recorder._key_injected, False)
        recorder._mouse_filter(0x0201, types.SimpleNamespace(flags=0x01))
        self.assertIs(recorder._mouse_injected, True)


class UtcTimes(unittest.TestCase):
    def test_trajectory_timestamps_are_utc(self):
        with tempfile.TemporaryDirectory() as root:
            episode = Path(root) / "episode"
            shutil.copytree(Path(__file__).parent / "samples" / "demo_episode", episode)
            trajectory.generate(episode)
            steps = [json.loads(line) for line in (episode / "trajectory.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertTrue(steps)
            for step in steps:
                self.assertTrue(step["timestamp"].endswith("Z"), step["timestamp"])
                parsed = datetime.fromisoformat(step["timestamp"].replace("Z", "+00:00"))
                self.assertEqual(round(parsed.timestamp() * 1000), step["t_ms"])

    def test_adapter_log_lines_are_utc(self):
        with tempfile.TemporaryDirectory() as root:
            manager = software.SoftwareManager.__new__(software.SoftwareManager)
            manager.raw_dir = Path(root) / "raw"
            manager._log_lock = __import__("threading").Lock()
            manager.log("autocad", "attached")
            line = (manager.raw_dir / "adapters.log").read_text(encoding="utf-8")
            stamp = line.split(" ", 1)[0]
            self.assertTrue(stamp.endswith("Z"), line)
            datetime.fromisoformat(stamp.replace("Z", "+00:00"))


if __name__ == "__main__":
    unittest.main()
