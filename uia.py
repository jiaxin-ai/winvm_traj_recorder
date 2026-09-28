#!/usr/bin/env python3
"""Windows-only: queries the UIA element under a point, or the focused element.

Uses the `uiautomation` package (pure Python wrapper over UI Automation COM).
Every query is time-boxed to 2 seconds; on timeout or failure it returns
None rather than raising, so a caller can always fall back to `target: null`
without losing the action (per task.md).

Standalone test (on the Windows VM):
    python uia.py
Polls whatever is under the mouse cursor every 2 seconds and prints the
result, so you can move the mouse over different controls and see what gets
picked up.
"""
import sys
import threading
import time

if sys.platform == "win32":
    import uiautomation as auto

QUERY_TIMEOUT_S = 2.0


def _target_from_control(control):
    if control is None:
        return None
    try:
        rect = control.BoundingRectangle
        window = control.GetTopLevelControl()
        return {
            "source": "uia",
            "name": control.Name or "",
            "control_type": control.ControlTypeName or "",
            "automation_id": control.AutomationId or "",
            "window": window.Name if window else "",
            "bounding_rect": [rect.left, rect.top, rect.right, rect.bottom],
        }
    except Exception:
        return None


def _run_with_timeout(fn, timeout_s):
    """Runs fn() in a background thread; returns None if it doesn't finish
    in time (uiautomation has no built-in per-call timeout).

    Every new thread that touches UIA needs its own COM initialization
    (uiautomation raises "CoInitialize has not been called" otherwise) —
    UIAutomationInitializerInThread does that for the lifetime of the
    `with` block.
    """
    result = {}

    def worker():
        try:
            with auto.UIAutomationInitializerInThread():
                result["value"] = fn()
        except Exception:
            result["value"] = None

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        return None
    return result.get("value")


def query_at_point(x, y):
    """Returns a target dict for the UIA element at (x, y), or None."""
    if sys.platform != "win32":
        return None

    def do_query():
        control = auto.ControlFromPoint(x, y)
        return _target_from_control(control)

    return _run_with_timeout(do_query, QUERY_TIMEOUT_S)


def query_focused():
    """Returns a target dict for the currently focused UIA element, or None."""
    if sys.platform != "win32":
        return None

    def do_query():
        control = auto.GetFocusedControl()
        return _target_from_control(control)

    return _run_with_timeout(do_query, QUERY_TIMEOUT_S)


def main():
    if sys.platform != "win32":
        print("uia.py 只能在 Windows 上运行(需要 uiautomation 包和 UIA COM 接口)")
        return
    print("[uia] 每 2 秒查询一次鼠标位置的 UIA element,Ctrl+C 退出")
    while True:
        x, y = auto.GetCursorPos()
        target = query_at_point(x, y)
        print(f"({x},{y}) -> {target}")
        time.sleep(2)


if __name__ == "__main__":
    main()
