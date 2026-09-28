#!/usr/bin/env python3
"""Windows-only: points the PowerShell profile hook at the current episode.

The actual transcript capture happens inside PowerShell itself
(Start-Transcript, installed by install_profile.ps1) — this module's only
job is to write/clear the marker file that the profile hook reads to decide
where to write raw/terminal/*.txt. Parsing the resulting transcript is done
offline by trajectory.py (plain text, no Windows dependency).

Standalone test (on the Windows VM):
    python terminal.py <episode_dir>
Writes the marker file pointing at <episode_dir>/raw/terminal, waits for
Enter, then clears it — open a *new* PowerShell window in between and
confirm a transcript file appears under raw/terminal/.
"""
import sys
from pathlib import Path

MARKER_PATH = Path(r"C:\ProgramData\trajrec\current_episode.txt")


def set_current_episode(terminal_dir):
    """Writes the raw/terminal directory path so the profile hook picks it up.

    Always resolved to an absolute path: the profile hook reads this from a
    brand-new PowerShell process, whose working directory has nothing to do
    with the one this was written from, so a relative path would silently
    fail to resolve there.
    """
    MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    MARKER_PATH.write_text(str(Path(terminal_dir).resolve()), encoding="utf-8")


def clear_current_episode():
    try:
        MARKER_PATH.unlink()
    except FileNotFoundError:
        pass


def main():
    if sys.platform != "win32":
        print("terminal.py 只能在 Windows 上运行(写入 C:\\ProgramData 下的标记文件)")
        return
    if len(sys.argv) < 2:
        print("用法: python terminal.py <episode_dir>")
        return
    terminal_dir = (Path(sys.argv[1]) / "raw" / "terminal").resolve()
    terminal_dir.mkdir(parents=True, exist_ok=True)
    set_current_episode(terminal_dir)
    print(f"[terminal] 已写入 {MARKER_PATH} -> {terminal_dir}")
    input("在此期间打开一个新 PowerShell 窗口、执行几条命令,然后回车结束测试...")
    clear_current_episode()
    print(f"[terminal] 已清除 {MARKER_PATH},请检查 {terminal_dir} 下是否生成了 transcript 文件")


if __name__ == "__main__":
    main()
