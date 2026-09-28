#!/usr/bin/env python3
"""Windows-only: watches a directory tree and reports created/modified/deleted/renamed files.

Uses the `watchdog` package. Only regular files are reported (directory
events are ignored). The watched directory must be passed explicitly by the
caller — this module never defaults to watching a whole drive.

Standalone test (on the Windows VM):
    python files.py --watch C:\\task
Create, edit, delete and rename files under that directory in another
window and watch the events print here.
"""
import argparse
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer


def start_watch(watch_dir, callback):
    """Watches watch_dir recursively; calls callback(event) for each change,
    where event is {"op", "path", "new_path"?, "size"?}. Returns the
    watchdog Observer (call .stop() + .join() to shut it down).

    The returned Observer also carries a `last_event_ms` attribute (int ms,
    None until the first event), updated on every change before the
    callback runs. snapshot.py polls this to detect when the watched
    directory has gone quiet, without needing its own separate watcher."""

    observer = Observer()
    observer.last_event_ms = None

    class Handler(FileSystemEventHandler):
        def on_created(self, event):
            if event.is_directory:
                return
            observer.last_event_ms = _now_ms()
            callback({"op": "created", "path": event.src_path, "size": _safe_size(event.src_path)})

        def on_modified(self, event):
            if event.is_directory:
                return
            observer.last_event_ms = _now_ms()
            callback({"op": "modified", "path": event.src_path, "size": _safe_size(event.src_path)})

        def on_deleted(self, event):
            if event.is_directory:
                return
            observer.last_event_ms = _now_ms()
            callback({"op": "deleted", "path": event.src_path})

        def on_moved(self, event):
            if event.is_directory:
                return
            observer.last_event_ms = _now_ms()
            callback({"op": "renamed", "path": event.src_path, "new_path": event.dest_path,
                      "size": _safe_size(event.dest_path)})

    def _safe_size(path):
        try:
            return Path(path).stat().st_size
        except OSError:
            return None

    observer.schedule(Handler(), str(watch_dir), recursive=True)
    observer.start()
    return observer


def _now_ms():
    return int(time.time() * 1000)


def main():
    if sys.platform != "win32":
        print("files.py 只能在 Windows 上运行(需要 watchdog)")
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", required=True, help="要监控的目录,如 C:\\task")
    args = parser.parse_args()
    print(f"[files] 监控目录: {args.watch},Ctrl+C 退出")
    start_watch(args.watch, lambda e: print(e))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
