#!/usr/bin/env python3
"""Windows-only: global mouse/keyboard hook that writes raw events.

Uses `pynput` for the low-level hooks. The hook callbacks themselves only
timestamp the event and put it on a queue — UIA queries and screenshots run
on two SEPARATE background worker threads (their own queues), per task.md's
constraint that hook callbacks must not do anything expensive. Screenshots
get their own thread specifically so a slow UIA query (up to 2s) never
delays the next screenshot behind it.

Writes to raw/events.jsonl:
    mouse_down, mouse_up, scroll, key_down, key_up   (always)
    uia_query    — queried at mouse_down, via uia.query_at_point
    uia_focused  — queried at the start of what looks like a typing burst,
                   via uia.query_focused (a heuristic hint; trajectory.py's
                   own merge logic decides the real type_text boundaries)
    screenshot   — two kinds, both written as {"type": "screenshot",
                   "path": ..., "kind": "trigger"|"settle"}:

      - "trigger": taken right as an action fires (mouse_down, first tick
        of a scroll burst, or almost any key_down). trajectory.py matches
        this to the action's own `action.screenshot` field — a picture of
        the exact moment of the click/keypress, e.g. to see precisely
        where the cursor landed.
      - "settle": taken once input has been quiet for SETTLE_DELAY_MS
        (every mouse/key event resets this debounce timer). This is what
        trajectory.py uses for `observation.screenshot`: the trajectory is
        Observation -> Action -> (effects settle) -> next Observation, so
        the observation for a step must show state AFTER the PREVIOUS
        action's effects finished, not the instant this step's own action
        fires (by then the mouse has already moved to its target, which
        would leak the action's own destination into what's supposed to
        be the "before" picture). The debounce naturally lands the
        "settle" shot right after an action — or a whole typing/scrolling
        burst — has finished and the UI had a moment to react.
        trajectory.py picks it up with "most recent settle screenshot at
        or before this action's start".

This module does NOT decide the final action boundaries (click vs drag,
hotkey vs type_text, etc.) — that merging happens offline in trajectory.py
from these raw events.

Standalone test (on the Windows VM):
    python input_recorder.py
Click around and type; each raw event line is printed as it's captured
(and also appended to ./raw_test/events.jsonl).
"""
import json
import queue
import sys
import threading
import time
from pathlib import Path

if sys.platform == "win32":
    from pynput import keyboard, mouse

import uia
import window as window_mod

TYPE_BURST_GAP_MS = 1500
SETTLE_DELAY_MS = 1000  # how long input must be quiet before we treat the UI as "settled" and take a screenshot
SCROLL_BURST_GAP_MS = 400  # matches trajectory.py's SCROLL_MERGE_GAP_MS; only the first tick of a burst gets a "trigger" shot

# pynput key -> task.md's lowercase key name convention
_SPECIAL_KEY_NAMES = {
    "enter": "enter", "tab": "tab", "esc": "esc", "space": "space",
    "backspace": "backspace", "delete": "delete", "up": "up", "down": "down",
    "left": "left", "right": "right", "home": "home", "end": "end",
    "page_up": "pageup", "page_down": "pagedown", "caps_lock": "capslock",
    "f1": "f1", "f2": "f2", "f3": "f3", "f4": "f4", "f5": "f5", "f6": "f6",
    "f7": "f7", "f8": "f8", "f9": "f9", "f10": "f10", "f11": "f11", "f12": "f12",
    "ctrl_l": "ctrl", "ctrl_r": "ctrl", "alt_l": "alt", "alt_r": "alt",
    "shift_l": "shift", "shift_r": "shift", "cmd": "win", "cmd_l": "win", "cmd_r": "win",
}


def key_to_name(key):
    """Maps a pynput key event to a lowercase key name (task.md convention)."""
    if isinstance(key, keyboard.KeyCode):
        if key.char:
            return key.char.lower()
        return f"vk{key.vk}"
    name = key.name if hasattr(key, "name") else str(key)
    return _SPECIAL_KEY_NAMES.get(name, name)


class JsonlWriter:
    """Thread-safe append-only writer for raw/events.jsonl."""

    def __init__(self, path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, event):
        line = json.dumps(event, ensure_ascii=False)
        with self._lock:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        return line


def now_ms():
    return int(time.time() * 1000)


class InputRecorder:
    """Owns the mouse/keyboard hooks and the background worker for
    UIA/screenshot side work. Call start()/stop()."""

    def __init__(self, writer, screenshot_dir=None, take_screenshot=None, on_event=None):
        self._writer = writer
        self._screenshot_dir = Path(screenshot_dir) if screenshot_dir else None
        self._take_screenshot = take_screenshot  # callable(path) -> None; injected so this module doesn't hard-depend on screen.py's mss usage
        self._on_event = on_event  # optional callback(event) for standalone/testing output
        self._shot_q = queue.Queue()
        self._uia_q = queue.Queue()
        self._shot_worker_thread = None
        self._uia_worker_thread = None
        self._shot_counter = 0
        self._shot_lock = threading.Lock()
        self._last_key_t_ms = None
        self._last_scroll_t_ms = None
        self._settle_timer = None
        self._settle_lock = threading.Lock()
        self._mouse_listener = None
        self._keyboard_listener = None
        self._enabled = threading.Event()  # gate: ignore input until main.py says "ready"

    # -- public control -----------------------------------------------
    def start(self):
        self._shot_worker_thread = threading.Thread(target=self._shot_worker_loop, daemon=True)
        self._uia_worker_thread = threading.Thread(target=self._uia_worker_loop, daemon=True)
        self._shot_worker_thread.start()
        self._uia_worker_thread.start()
        self._mouse_listener = mouse.Listener(on_click=self._on_click, on_scroll=self._on_scroll)
        self._keyboard_listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._mouse_listener.start()
        self._keyboard_listener.start()

    def stop(self):
        if self._mouse_listener:
            self._mouse_listener.stop()
        if self._keyboard_listener:
            self._keyboard_listener.stop()
        with self._settle_lock:
            if self._settle_timer:
                self._settle_timer.cancel()
                self._settle_timer = None
        self._shot_q.put(None)
        self._uia_q.put(None)
        # drains any already-queued screenshot jobs first, so a caller
        # taking one more screenshot right after stop() can't race the
        # worker thread over the next screenshot index. The UIA worker can
        # be slow (up to 2s/query) and doesn't affect screenshot indices,
        # so it gets a shorter grace period rather than blocking stop().
        if self._shot_worker_thread:
            self._shot_worker_thread.join(timeout=5)
        if self._uia_worker_thread:
            self._uia_worker_thread.join(timeout=3)

    def next_screenshot_index(self):
        with self._shot_lock:
            idx = self._shot_counter
            self._shot_counter += 1
            return idx

    def set_enabled(self, enabled):
        """main.py flips this on after the ready prompt; input before that is ignored."""
        if enabled:
            self._enabled.set()
        else:
            self._enabled.clear()

    # -- event emission --------------------------------------------------
    def _emit(self, event):
        line_event = {"t_ms": now_ms(), **event}
        self._writer.write(line_event)
        if self._on_event:
            self._on_event(line_event)

    def _queue_screenshot(self, kind):
        if self._screenshot_dir is None or self._take_screenshot is None:
            return
        with self._shot_lock:
            idx = self._shot_counter
            self._shot_counter += 1
        abs_path = self._screenshot_dir / f"{idx:06d}.png"
        # The raw event stores a path relative to the episode directory
        # (like main.py's own initial/final screenshots) so the whole
        # episode directory stays portable — trajectory.html references
        # screenshots by this relative path, not the absolute one used to
        # actually write the file.
        rel_path = f"screenshots/{idx:06d}.png"
        self._shot_q.put((abs_path, rel_path, kind))

    def _touch_activity(self):
        """Reschedules the settle-screenshot timer. Called on every raw
        mouse/key event so the timer only actually fires once input has
        been quiet for SETTLE_DELAY_MS — i.e. once whatever the user just
        did (a click, a drag, a whole typing/scrolling burst) is over and
        the UI has had a moment to react."""
        with self._settle_lock:
            if self._settle_timer:
                self._settle_timer.cancel()
            self._settle_timer = threading.Timer(SETTLE_DELAY_MS / 1000, self._queue_screenshot, args=("settle",))
            self._settle_timer.daemon = True
            self._settle_timer.start()

    # -- mouse -------------------------------------------------------
    def _on_click(self, x, y, button, pressed):
        if not self._enabled.is_set():
            return
        btn = {"left": "left", "right": "right", "middle": "middle"}.get(button.name, button.name)
        if pressed:
            self._emit({"type": "mouse_down", "button": btn, "x": x, "y": y})
            self._queue_screenshot("trigger")
            self._uia_q.put(("uia_query", x, y))
        else:
            self._emit({"type": "mouse_up", "button": btn, "x": x, "y": y})
        self._touch_activity()

    def _on_scroll(self, x, y, dx, dy):
        if not self._enabled.is_set():
            return
        t_ms = now_ms()
        is_new_burst = self._last_scroll_t_ms is None or (t_ms - self._last_scroll_t_ms) > SCROLL_BURST_GAP_MS
        self._last_scroll_t_ms = t_ms
        self._emit({"type": "scroll", "x": x, "y": y, "dx": dx, "dy": dy})
        if is_new_burst:
            self._queue_screenshot("trigger")
        self._touch_activity()

    # -- keyboard ------------------------------------------------------
    def _on_press(self, key):
        if not self._enabled.is_set():
            return
        name = key_to_name(key)
        t_ms = now_ms()
        is_new_burst = self._last_key_t_ms is None or (t_ms - self._last_key_t_ms) > TYPE_BURST_GAP_MS
        self._last_key_t_ms = t_ms
        self._emit({"type": "key_down", "key": name})
        if name not in ("ctrl", "alt", "shift", "win"):
            # Screenshot on (almost) every key, not just burst starts: a
            # standalone key right after a typing burst (e.g. Enter to
            # confirm) becomes its own action in trajectory.py's merge
            # logic even though it isn't a "new burst" by this simple gap
            # timer, so it still needs a trigger screenshot close to it.
            self._queue_screenshot("trigger")
        self._touch_activity()
        if is_new_burst:
            win = window_mod.get_active_window() if sys.platform == "win32" else None
            is_powershell = bool(win and win.get("process", "").lower() in ("powershell.exe", "pwsh.exe"))
            if not is_powershell:
                self._uia_q.put(("uia_focused",))

    def _on_release(self, key):
        if not self._enabled.is_set():
            return
        self._emit({"type": "key_up", "key": key_to_name(key)})
        self._touch_activity()

    # -- background workers ----------------------------------------------
    def _shot_worker_loop(self):
        while True:
            job = self._shot_q.get()
            if job is None:
                return
            abs_path, rel_path, kind = job
            try:
                self._take_screenshot(abs_path)
                self._emit({"type": "screenshot", "path": rel_path, "kind": kind})
            except Exception as exc:
                print(f"[input_recorder] screenshot worker error: {exc}", file=sys.stderr)

    def _uia_worker_loop(self):
        while True:
            job = self._uia_q.get()
            if job is None:
                return
            kind = job[0]
            try:
                if kind == "uia_query":
                    _, x, y = job
                    target = uia.query_at_point(x, y)
                    self._emit({"type": "uia_query", "x": x, "y": y, "target": target})
                elif kind == "uia_focused":
                    target = uia.query_focused()
                    self._emit({"type": "uia_focused", "target": target})
            except Exception as exc:
                print(f"[input_recorder] uia worker error: {exc}", file=sys.stderr)


def main():
    if sys.platform != "win32":
        print("input_recorder.py 只能在 Windows 上运行(需要 pynput)")
        return
    out_dir = Path("raw_test")
    writer = JsonlWriter(out_dir / "events.jsonl")
    shot_dir = out_dir / "screenshots"
    shot_dir.mkdir(parents=True, exist_ok=True)

    import screen
    recorder = InputRecorder(
        writer,
        screenshot_dir=shot_dir,
        take_screenshot=screen.take_screenshot,
        on_event=lambda e: print(e),
    )
    recorder.set_enabled(True)
    recorder.start()
    print(f"[input_recorder] 录制中,写入 {out_dir}/events.jsonl,Ctrl+C 退出")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        recorder.stop()


if __name__ == "__main__":
    main()
