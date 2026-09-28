#!/usr/bin/env python3
"""Windows-only: polls the process list and reports start/exit as a diff.

Writes every process it sees (including system/background ones) — filtering
down to "current user, non-background" is trajectory.py's job when it builds
the final trajectory, not this module's. A short-lived process that starts
and exits entirely between two polls can be missed; task.md accepts this
("允许遗漏极短命的进程").

Standalone test (on the Windows VM):
    python processes.py
Prints start/exit events as they happen; launch and close a program (e.g.
notepad) from another window to see it show up.
"""
import sys
import time

if sys.platform == "win32":
    import psutil

POLL_INTERVAL_S = 0.5


def _normalize_user(raw_username):
    """psutil returns e.g. "NT AUTHORITY\\SYSTEM" or "WIN-VM\\demo" on
    Windows; strip the domain/machine prefix so trajectory.py can compare
    against plain names like "SYSTEM"."""
    if not raw_username:
        return ""
    return raw_username.split("\\")[-1]


def _snapshot():
    procs = {}
    for p in psutil.process_iter(["pid", "name", "cmdline", "username"]):
        try:
            info = p.info
            procs[info["pid"]] = {
                "pid": info["pid"],
                "name": info["name"] or "",
                "cmdline": " ".join(info["cmdline"]) if info["cmdline"] else "",
                "user": _normalize_user(info["username"]),
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return procs


def poll_processes(callback, interval_s=POLL_INTERVAL_S, stop_event=None):
    """Calls callback(event) for each start/exit, where event is
    {"op": "start"|"exit", "name", "cmdline", "pid", "user"}."""
    prev = _snapshot()
    while stop_event is None or not stop_event.is_set():
        time.sleep(interval_s)
        cur = _snapshot()
        for pid, info in cur.items():
            if pid not in prev:
                callback({"op": "start", "name": info["name"], "cmdline": info["cmdline"],
                           "pid": pid, "user": info["user"]})
        for pid, info in prev.items():
            if pid not in cur:
                callback({"op": "exit", "name": info["name"], "pid": pid, "user": info["user"]})
        prev = cur


def main():
    if sys.platform != "win32":
        print("processes.py 只能在 Windows 上运行(需要 psutil)")
        return
    print("[processes] 每 0.5 秒轮询一次进程列表,Ctrl+C 退出")
    try:
        poll_processes(lambda e: print(e))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
