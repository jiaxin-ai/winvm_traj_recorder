#!/usr/bin/env python3
"""Windows-only: polls the foreground window's title and owning process name.

Standalone test (on the Windows VM):
    python window.py
Prints the active window every second; switch focus between a few apps to
confirm title/process come back correctly.
"""
import sys
import time

if sys.platform == "win32":
    import psutil
    import win32gui
    import win32process

POLL_INTERVAL_S = 0.2


def get_active_window():
    """Returns {"title": ..., "process": ...} for the foreground window, or None."""
    if sys.platform != "win32":
        return None
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return None
    title = win32gui.GetWindowText(hwnd)
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        process_name = psutil.Process(pid).name()
    except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
        process_name = ""
    return {"title": title, "process": process_name}


def poll_loop(callback, interval_s=POLL_INTERVAL_S, stop_event=None):
    """Calls callback(window) whenever the active window changes.

    Runs until stop_event is set (a threading.Event); if stop_event is None,
    runs forever.
    """
    last = None
    while stop_event is None or not stop_event.is_set():
        win = get_active_window()
        if win != last:
            callback(win)
            last = win
        time.sleep(interval_s)


def main():
    if sys.platform != "win32":
        print("window.py 只能在 Windows 上运行(需要 pywin32 和 psutil)")
        return
    print("[window] 每秒打印一次前台窗口,Ctrl+C 退出")
    last = None
    try:
        while True:
            win = get_active_window()
            if win != last:
                print(win)
                last = win
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
