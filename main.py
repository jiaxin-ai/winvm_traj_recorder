#!/usr/bin/env python3
"""Windows-only: starts, coordinates and stops one recording episode.

    python main.py --output C:\\traj --task task.json --watch C:\\task

Creates output/<episode_id>/ (episode_id defaults to the start time), starts
every collection module, records the initial observation, then waits for
Ctrl+C or the C:\\ProgramData\\trajrec\\STOP file. On stop it shuts every
module down cleanly, copies changed files from --watch into artifacts/, and
calls trajectory.py to build trajectory.jsonl / trajectory.html.

See README.md for what is and isn't verified on real Windows.
"""
import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    import ctypes
    # Must happen before any window/UIA/screenshot call, so mouse
    # coordinates, UIA rects and screenshot pixels all agree (task.md).
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception as exc:
        print(f"[main] SetProcessDpiAwareness failed: {exc}", file=sys.stderr)

import files
import input_recorder
import processes
import screen
import snapshot
import software
import terminal
import trajectory
import window

STOP_MARKER = Path(r"C:\ProgramData\trajrec\STOP")
ARTIFACT_SIZE_LIMIT = 500 * 1024 * 1024  # 500MB, per task.md
WINDOW_POLL_INTERVAL_S = 0.2
PROCESS_POLL_INTERVAL_S = 0.5


def make_episode_dir(output_root):
    episode_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    episode_dir = Path(output_root) / episode_id
    for sub in ("screenshots", "artifacts", "raw", "raw/terminal"):
        (episode_dir / sub).mkdir(parents=True, exist_ok=True)
    return episode_dir


def copy_task_file(task_path, episode_dir):
    shutil.copyfile(task_path, episode_dir / "task.json")


def check_watch_output_nesting(output_root, watch_dir):
    """task-v1.1.md: if --output sits inside --watch, snapshot commits
    would capture the trajectory's own output as it's written. Fail fast
    instead of silently corrupting the recording."""
    output_abs = Path(output_root).resolve()
    watch_abs = Path(watch_dir).resolve()
    if _is_relative_to(output_abs, watch_abs):
        print(f"[trajrec] 错误: --output ({output_abs}) 位于 --watch ({watch_abs}) 之内,"
              f"轨迹数据会被写进快照,请把 --output 放到 --watch 目录之外", file=sys.stderr)
        sys.exit(1)


def write_meta(episode_dir, watch_dir, snapshot_mgr, software_mgr):
    meta = {
        "watch_dir": str(Path(watch_dir).resolve()),
        "snapshot": {"enabled": snapshot_mgr.enabled, "reason": snapshot_mgr.disabled_reason},
    }
    if software_mgr.enabled:
        meta["adapters"] = software_mgr.status()
    (Path(episode_dir) / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


class Episode:
    """Owns every collection module's lifecycle for one recording."""

    def __init__(self, episode_dir, watch_dir, snapshot_enabled=True, adapters="auto"):
        self.episode_dir = Path(episode_dir)
        self.watch_dir = watch_dir
        self.writer = input_recorder.JsonlWriter(self.episode_dir / "raw" / "events.jsonl")
        self.snapshot_mgr = snapshot.SnapshotManager(
            self.episode_dir, Path(watch_dir), self.writer, enabled=snapshot_enabled)
        self.snapshot_mgr.init_repo()  # resolved eagerly so main() can write meta.json before start()
        self.software_mgr = software.SoftwareManager(self.episode_dir, software.parse_adapters_arg(adapters))
        self.software_mgr.load()
        self.recorder = screen.Recorder()
        self.input_rec = input_recorder.InputRecorder(
            self.writer,
            screenshot_dir=self.episode_dir / "screenshots",
            take_screenshot=screen.take_screenshot,
            on_event=self._on_input_event,
            on_milestone=lambda t_ms: self.snapshot_mgr.notify_milestone(None, t_ms),
        )
        self._window_stop = None
        self._process_stop = None
        self._file_observer = None
        self._window_thread = None
        self._process_thread = None

    def _write(self, event):
        self.writer.write(event)

    def start(self):
        # 1. recording + all background modules
        self.recorder.start(self.episode_dir / "recording.mp4")
        self._write({"t_ms": input_recorder.now_ms(), "type": "recording_start", "path": "recording.mp4"})

        self.software_mgr.start()  # before window polling, so the first foreground report can attach

        import threading
        self._window_stop = threading.Event()
        self._window_thread = threading.Thread(
            target=window.poll_loop,
            args=(self._on_window, WINDOW_POLL_INTERVAL_S, self._window_stop),
            daemon=True,
        )
        self._window_thread.start()

        self._process_stop = threading.Event()
        self._process_thread = threading.Thread(
            target=processes.poll_processes,
            args=(self._on_process, PROCESS_POLL_INTERVAL_S, self._process_stop),
            daemon=True,
        )
        self._process_thread.start()

        self._file_observer = files.start_watch(self.watch_dir, self._on_file)
        if self.snapshot_mgr.enabled:
            self.snapshot_mgr.start(self._file_observer)

        terminal.set_current_episode(self.episode_dir / "raw" / "terminal")

        self.input_rec.start()  # hooks are live but set_enabled(False) until ready

        # 2. initial observation. Uses the same shared counter as
        # input_rec's own action-triggered screenshots (next_screenshot_index)
        # so this always gets index 0 and nothing else can collide with it.
        shot_idx = self.input_rec.next_screenshot_index()
        shot_path = self.episode_dir / "screenshots" / f"{shot_idx:06d}.png"
        screen.take_screenshot(shot_path)
        self._write({"t_ms": input_recorder.now_ms(), "type": "screenshot",
                     "path": f"screenshots/{shot_idx:06d}.png", "kind": "settle"})
        self.software_mgr.notify_observation()
        win = window.get_active_window()
        if win:
            self._write({"t_ms": input_recorder.now_ms(), "type": "window_active", **win})

        # 3. ready
        print("[trajrec] 初始状态已记录,可以开始操作")

        # 4. only now does input count as actions
        self.input_rec.set_enabled(True)

    def _on_window(self, win):
        self.software_mgr.notify_foreground(win["process"] if win else None)
        if win is None:
            return
        self._write({"t_ms": input_recorder.now_ms(), "type": "window_active", **win})

    def _on_process(self, event):
        self._write({"t_ms": input_recorder.now_ms(), "type": "process_event", **event})
        if event.get("op") == "exit":
            self.software_mgr.notify_process_exit(event.get("name"))
        self.software_mgr.notify_drain()

    def _on_file(self, event):
        t_ms = input_recorder.now_ms()
        self._write({"t_ms": t_ms, "type": "file_event", **event})
        if self.snapshot_mgr.enabled:
            self.snapshot_mgr.notify_file_changed(t_ms)
        self.software_mgr.notify_drain()

    def _on_input_event(self, event):
        """Runs on the hook / screenshot-worker thread right after the raw
        event is written; notify_*() only sets a flag, never blocks."""
        if event["type"] == "screenshot":
            if event.get("kind") == "settle":
                self.software_mgr.notify_observation()
        elif event["type"] in ("mouse_down", "mouse_up", "scroll", "key_down", "key_up"):
            self.software_mgr.notify_drain()

    def stop(self):
        self.input_rec.stop()
        if self._window_stop:
            self._window_stop.set()
        if self._process_stop:
            self._process_stop.set()
        if self._file_observer:
            self._file_observer.stop()
            self._file_observer.join()
        if self.snapshot_mgr.enabled:
            self.snapshot_mgr.finalize()

        # final screenshot + window state before closing the recording
        final_idx = self.input_rec.next_screenshot_index()
        final_shot = self.episode_dir / "screenshots" / f"{final_idx:06d}.png"
        screen.take_screenshot(final_shot)
        self._write({"t_ms": input_recorder.now_ms(), "type": "screenshot",
                     "path": f"screenshots/{final_idx:06d}.png", "kind": "settle"})
        self.software_mgr.notify_observation()
        win = window.get_active_window()
        if win:
            self._write({"t_ms": input_recorder.now_ms(), "type": "window_active", **win})

        # last get_actions()/get_events(), then detach() everything
        self.software_mgr.stop()

        self.recorder.stop()
        self._write({"t_ms": input_recorder.now_ms(), "type": "recording_end"})

        terminal.clear_current_episode()


def copy_artifacts(episode_dir, watch_dir):
    """Copies files that raw/events.jsonl shows as created/modified during
    the recording into artifacts/, skipping anything over 500MB (the file
    event itself already stayed in raw/events.jsonl either way)."""
    raw_events = trajectory.load_raw_events(episode_dir / "raw")
    changed_paths = set()
    for e in raw_events:
        if e["type"] == "file_event" and e["op"] in ("created", "modified"):
            changed_paths.add(e["path"])
        if e["type"] == "file_event" and e["op"] == "renamed":
            changed_paths.add(e["new_path"])

    watch_dir = Path(watch_dir)
    artifacts_dir = episode_dir / "artifacts"
    for path_str in changed_paths:
        src = Path(path_str.replace("/", "\\")) if sys.platform == "win32" else Path(path_str)
        try:
            if not src.exists() or not src.is_file():
                continue
            if src.stat().st_size > ARTIFACT_SIZE_LIMIT:
                print(f"[main] 跳过 artifact(超过 500MB): {src}")
                continue
            rel = src.relative_to(watch_dir) if _is_relative_to(src, watch_dir) else src.name
            dst = artifacts_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        except Exception as exc:
            print(f"[main] 复制 artifact 失败 {src}: {exc}", file=sys.stderr)


def _is_relative_to(path, base):
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def wait_for_stop():
    STOP_MARKER.parent.mkdir(parents=True, exist_ok=True)
    try:
        while True:
            if STOP_MARKER.exists():
                STOP_MARKER.unlink()
                print("[trajrec] 检测到 STOP 文件,停止录制")
                return
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("[trajrec] 收到 Ctrl+C,停止录制")
        return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="输出根目录,如 C:\\traj")
    parser.add_argument("--task", required=True, help="task.json 文件路径")
    parser.add_argument("--watch", required=True, help="文件监控目录,如 C:\\task")
    parser.add_argument("--snapshot", dest="snapshot", action="store_true", default=True,
                         help="启用文件快照(默认开启)")
    parser.add_argument("--no-snapshot", dest="snapshot", action="store_false",
                         help="关闭文件快照功能")
    parser.add_argument("--adapters", default="auto",
                         help="软件适配器:auto(默认,按前台进程自动匹配)、none(禁用)、"
                              "或逗号分隔的 adapters/ 子目录名,如 mock,autocad")
    args = parser.parse_args()

    if sys.platform != "win32":
        print("main.py 只能在 Windows 上运行(依赖 pynput/uiautomation/pywin32/mss/watchdog)")
        sys.exit(1)

    check_watch_output_nesting(args.output, args.watch)

    episode_dir = make_episode_dir(args.output)
    copy_task_file(args.task, episode_dir)
    print(f"[trajrec] episode 目录: {episode_dir}")

    episode = Episode(episode_dir, args.watch, snapshot_enabled=args.snapshot, adapters=args.adapters)
    write_meta(episode_dir, args.watch, episode.snapshot_mgr, episode.software_mgr)
    if not episode.snapshot_mgr.enabled:
        print(f"[trajrec] 文件快照未启用: {episode.snapshot_mgr.disabled_reason}")
    if episode.software_mgr.enabled:
        names = [a.name for a in episode.software_mgr.adapters] or "无"
        print(f"[trajrec] 已加载软件适配器: {names}")
    episode.start()

    wait_for_stop()

    episode.stop()
    write_meta(episode_dir, args.watch, episode.snapshot_mgr, episode.software_mgr)  # final adapter counters
    copy_artifacts(episode_dir, args.watch)

    print("[trajrec] 生成 trajectory.jsonl / trajectory.html ...")
    trajectory.generate(episode_dir)
    print(f"[trajrec] 完成: {episode_dir}")


if __name__ == "__main__":
    main()
