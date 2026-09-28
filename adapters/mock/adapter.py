#!/usr/bin/env python3
"""MockCAD: a fake adapter for checking the Recorder's adapter plumbing
without any engineering software installed. Treats notepad.exe as "the
software". Also the smallest working example of the adapter interface in
software_trajectory_collector_specification.md.

Records are synthesized when get_actions()/get_events() are called, so
their t_ms is the moment the Recorder asked -- a real adapter must instead
stamp t_ms when the software event happens.

MOCK_FAULT (environment variable) injects faults to exercise the Recorder's
isolation:
    slow   get_state() sleeps 2 s (budget is 500 ms)
    crash  get_state()/get_actions()/get_events() raise (attach still
           succeeds, so the consecutive-failure limit is what disables it)
    flood  get_events() returns 5000 records per call (the spec's per-call
           limit is 200)

Debug entry (spec 3.3), from the project root:
    python -m adapters.mock --probe
    python -m adapters.mock --watch
"""
import argparse
import json
import os
import sys
import time

FLOOD_SIZE = 5000


def _now_ms():
    return time.time_ns() // 1_000_000


def _notepad_title():
    """Title of the first visible notepad.exe window; None if there isn't
    one or this isn't Windows."""
    if sys.platform != "win32":
        return None
    try:
        import psutil
        import win32gui
        import win32process
    except ImportError:
        return None
    titles = []

    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if psutil.Process(pid).name().lower() == "notepad.exe":
                title = win32gui.GetWindowText(hwnd)
                if title:
                    titles.append(title)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(visit, None)
    except Exception:
        return None
    return titles[0] if titles else None


class Adapter:
    NAME = "MockCAD"
    PROCESS_NAMES = ["notepad.exe"]
    SPEC_VERSION = "1"

    def __init__(self):
        self._ctx = None
        self._fault = os.environ.get("MOCK_FAULT", "").strip().lower()
        self._action_seq = 0
        self._event_seq = 0

    def attach(self, ctx):
        self._ctx = ctx
        ctx.log(f"MockCAD attach (MOCK_FAULT={self._fault or 'none'})")
        return True

    def detach(self):
        self._ctx = None

    def get_state(self):
        if self._fault == "slow":
            time.sleep(2)
        self._crash_if_asked("get_state")
        return {
            "t_ms": _now_ms(),
            "software": self.NAME,
            "active_document": _notepad_title(),
            "selection": [],
            "mode": "idle",
            "action_count": self._action_seq,
        }

    def get_actions(self):
        self._crash_if_asked("get_actions")
        self._action_seq += 1
        return [self._record("command", "MOCK_COMMAND", {"seq": self._action_seq},
                             f"MOCK_COMMAND #{self._action_seq}")]

    def get_events(self):
        self._crash_if_asked("get_events")
        if self._fault == "flood":
            return [self._record("command_executed", "MOCK_FLOOD", {"index": i}, f"MOCK_FLOOD #{i}")
                    for i in range(FLOOD_SIZE)]
        self._event_seq += 1
        return [self._record("command_executed", "MOCK_COMMAND", {"seq": self._event_seq},
                             f"MOCK_COMMAND #{self._event_seq} done")]

    def _record(self, type_, name, params, raw):
        return {"t_ms": _now_ms(), "software": self.NAME, "type": type_, "name": name,
                "params": params, "source": "mock", "raw": raw}

    def _crash_if_asked(self, method):
        if self._fault == "crash":
            raise RuntimeError(f"MOCK_FAULT=crash in {method}()")


def main(argv=None):
    parser = argparse.ArgumentParser(description="MockCAD adapter debug entry")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--probe", action="store_true", help="attach, print one get_state() and its duration")
    mode.add_argument("--watch", action="store_true", help="print get_actions()/get_events() every 500 ms")
    args = parser.parse_args(argv)

    class _Ctx:
        episode_dir = None

        def log(self, msg):
            print(f"[ctx.log] {msg}")

    adapter = Adapter()
    print(f"attach -> {adapter.attach(_Ctx())}")
    try:
        if args.probe:
            t0 = time.perf_counter()
            state = adapter.get_state()
            print(f"get_state ({(time.perf_counter() - t0) * 1000:.1f} ms):")
            print(json.dumps(state, ensure_ascii=False, indent=2))
        else:
            while True:
                for record in adapter.get_actions() + adapter.get_events():
                    print(json.dumps(record, ensure_ascii=False))
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        adapter.detach()


if __name__ == "__main__":
    main()
