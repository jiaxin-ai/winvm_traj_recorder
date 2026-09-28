#!/usr/bin/env python3
"""Windows-only: screenshots (mss) and screen recording (ffmpeg + gdigrab).

Screenshots capture the whole virtual desktop at physical-pixel resolution,
so they line up 1:1 with the mouse/UIA coordinates recorded elsewhere (this
relies on main.py calling SetProcessDpiAwareness(2) before anything else
runs, per task.md).

Recording is a plain ffmpeg subprocess. stop_recording() sends it a
graceful quit (stdin 'q', like pressing q in the ffmpeg console) and waits,
instead of killing the process, so the mp4 is finalized correctly.

Standalone test (on the Windows VM):
    python screen.py
Takes one screenshot into the current directory and prints its path/size,
so you can confirm resolution and DPI look right.
"""
import subprocess
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    import mss

FFMPEG_QUIT_TIMEOUT_S = 10


def take_screenshot(out_path):
    """Captures the full virtual desktop to out_path (PNG). Returns out_path."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with mss.mss() as sct:
        # monitor 0 is the union of all monitors (the whole virtual desktop)
        shot = sct.grab(sct.monitors[0])
        mss.tools.to_png(shot.rgb, shot.size, output=str(out_path))
    return out_path


class Recorder:
    """Wraps one ffmpeg gdigrab process."""

    def __init__(self):
        self._proc = None

    def start(self, out_path, framerate=10):
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-y",
            "-f", "gdigrab",
            "-framerate", str(framerate),
            "-i", "desktop",
            "-vcodec", "libx264",
            "-pix_fmt", "yuv420p",
            str(out_path),
        ]
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self._proc

    def stop(self):
        if not self._proc:
            return
        try:
            self._proc.communicate(input=b"q", timeout=FFMPEG_QUIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None


def main():
    if sys.platform != "win32":
        print("screen.py 只能在 Windows 上运行(需要 mss 包)")
        return
    out = Path("screen_test.png")
    take_screenshot(out)
    print(f"[screen] 截图已保存: {out.resolve()} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
