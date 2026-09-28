#!/usr/bin/env python3
"""Restore or export a file snapshot from a recorded episode.

    python restore.py <episode_dir> --list
    python restore.py <episode_dir> --step 12 --export D:\\check       (default, safe)
    python restore.py <episode_dir> --step 12 --in-place
    python restore.py <episode_dir> --step 12 --in-place --kill

--commit <hash> works in place of --step. The work directory path is read
from snapshots/index.json's work_dir field; override with --work-dir if the
episode data was copied to another machine.

Export mode only reads git objects (via `git archive`) and never touches
the work directory -- it works on any machine. In-place mode overwrites the
work directory itself and only makes sense on the machine that actually has
it (usually the Windows VM); see README.md for why the software must be
closed first.
"""
import argparse
import io
import json
import sys
import tarfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import psutil

import snapshot

# Common engineering-software process names to scan for before an in-place
# restore, so the software's own in-memory state doesn't overwrite the
# restored files on its next save. Not exhaustive -- task-v1.1.md says this
# list should be configurable, so it's just a plain constant to edit.
SOFTWARE_PROCESS_NAMES = [
    "sldworks.exe", "sw.exe", "acad.exe", "acadlt.exe", "rhino.exe",
    "ansys.exe", "catia.exe", "inventor.exe", "nx.exe", "creo.exe",
    "fusion360.exe", "revit.exe", "3dsmax.exe", "maya.exe",
]

TZ = timezone(timedelta(hours=8))


def load_index(episode_dir):
    path = Path(episode_dir) / "snapshots" / "index.json"
    if not path.exists():
        print(f"[restore] 找不到 {path},该 episode 可能未启用快照功能", file=sys.stderr)
        sys.exit(1)
    return json.loads(path.read_text(encoding="utf-8"))


def load_trajectory_steps(episode_dir):
    path = Path(episode_dir) / "traj" / "trajectory.jsonl"
    steps = []
    if not path.exists():
        return steps
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                steps.append(json.loads(line))
    return steps


def resolve_commit(args, steps):
    if args.commit:
        return args.commit
    if args.step is None:
        print("[restore] 必须指定 --step 或 --commit", file=sys.stderr)
        sys.exit(1)
    matching = [s for s in steps if s["step_id"] == args.step]
    if not matching:
        print(f"[restore] trajectory.jsonl 中找不到 step_id={args.step}", file=sys.stderr)
        sys.exit(1)
    snap = matching[0].get("snapshot")
    if not snap or not snap.get("file_ckpt"):
        print(f"[restore] step {args.step} 没有对应的文件版本"
              f"(快照未启用,或录制开始后到这一步之前还没有过快照)", file=sys.stderr)
        sys.exit(1)
    return snap["file_ckpt"]


def list_checkpoints(index):
    if not index.get("enabled"):
        print("[restore] 该 episode 未启用快照功能")
        return
    checkpoints = index.get("checkpoints", [])
    if not checkpoints:
        print("[restore] 没有任何快照记录")
        return
    print(f"{'step':>5}  {'commit':<13}{'reason':<14}{'name':<16}{'requested_t':<31}files_changed")
    for c in checkpoints:
        ts = datetime.fromtimestamp(c["requested_t_ms"] / 1000, tz=TZ).isoformat(timespec="milliseconds")
        name = c.get("name") or ""
        files = ",".join(c.get("files_changed") or [])
        step_id = c.get("step_id")
        step_str = "" if step_id is None else str(step_id)
        print(f"{step_str:>5}  {c['commit']:<13}{c['reason']:<14}{name:<16}{ts:<31}{files}")


def export_commit(git_dir, work_dir, commit, target_dir):
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    r = snapshot.run_git(git_dir, work_dir, ["archive", "--format=tar", commit], text=False)
    if r.returncode != 0:
        print(f"[restore] git archive 失败: {r.stderr.decode(errors='replace')}", file=sys.stderr)
        sys.exit(1)
    with tarfile.open(fileobj=io.BytesIO(r.stdout), mode="r:") as tf:
        try:
            tf.extractall(target_dir, filter="data")  # Python 3.12+; harmless here since we made the tar ourselves
        except TypeError:
            tf.extractall(target_dir)  # filter= didn't exist before 3.12
    print(f"[restore] 已导出 {commit} 到 {target_dir}")


def find_running_software(process_names):
    names_lower = {n.lower() for n in process_names}
    found = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            name = (p.info["name"] or "").lower()
            if name in names_lower:
                found.append((p.info["pid"], p.info["name"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


def kill_processes(pids, grace_s=3):
    procs = []
    for pid, name in pids:
        try:
            procs.append(psutil.Process(pid))
        except psutil.NoSuchProcess:
            continue
    for p in procs:
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(procs, timeout=5)
    for p in alive:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    # software still flushes lock/temp files for a moment after exiting
    time.sleep(grace_s)


def move_extra_files_to_discarded(git_dir, work_dir, commit):
    """Moves (never deletes) every file now on disk that isn't part of the
    restored commit -- i.e. files created after that version -- into
    _discarded/<timestamp>/, per task-v1.1.md.

    `git checkout -f <commit> -- .` only checks out paths that exist in
    <commit>; it does NOT remove a file that was added in a *later*
    commit (it stays tracked and clean in the index, so `git status`
    never reports it as untracked either) -- so this compares the actual
    file list on disk against `git ls-tree` of the target commit instead
    of trusting `git status`."""
    work_dir = Path(work_dir)
    r = snapshot.run_git(git_dir, work_dir, ["ls-tree", "-r", "--name-only", commit])
    if r.returncode != 0:
        print(f"[restore] git ls-tree 失败: {r.stderr}", file=sys.stderr)
        return []
    wanted = set(r.stdout.splitlines())

    moved = []
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    discard_root = work_dir / "_discarded" / stamp
    for path in work_dir.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(work_dir).as_posix()
        if rel.startswith("_discarded/"):
            continue
        if rel in wanted:
            continue
        dst = discard_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        path.rename(dst)
        moved.append(rel)
    return moved


def restore_in_place(git_dir, work_dir, commit, kill):
    work_dir = Path(work_dir)
    running = find_running_software(SOFTWARE_PROCESS_NAMES)
    if running:
        print("[restore] 检测到以下可能相关的软件进程,它们内存中的旧状态下次保存会覆盖恢复的文件:")
        for pid, name in running:
            print(f"    {name} (pid {pid})")
        if kill:
            print("[restore] 正在结束上述进程...")
            kill_processes(running)
        else:
            try:
                input("请先手动关闭上述软件,完成后按回车继续(Ctrl+C 取消): ")
            except KeyboardInterrupt:
                print("\n[restore] 已取消")
                sys.exit(1)

    r = snapshot.run_git(git_dir, work_dir, ["checkout", "-f", commit, "--", "."])
    if r.returncode != 0:
        print(f"[restore] git checkout 失败: {r.stderr}", file=sys.stderr)
        sys.exit(1)

    moved = move_extra_files_to_discarded(git_dir, work_dir, commit)

    print(f"[restore] 工作目录已恢复到 {commit}")
    if moved:
        print(f"[restore] {len(moved)} 个该版本之后新建的文件已移入 _discarded/: {', '.join(moved)}")
    print("[restore] 请重新打开软件和文件")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("episode_dir", help="episode 目录,如 output/20260922_103215")
    parser.add_argument("--list", action="store_true", help="列出所有版本")
    parser.add_argument("--step", type=int, help="按 step_id 定位版本")
    parser.add_argument("--commit", help="按 commit hash 定位版本(可代替 --step)")
    parser.add_argument("--export", metavar="DIR",
                         help="导出到指定目录(默认行为;不给目录时导出到 <episode_dir>/restore/restore_<commit>)")
    parser.add_argument("--in-place", action="store_true", help="就地恢复工作目录(会覆盖当前文件,谨慎使用)")
    parser.add_argument("--kill", action="store_true", help="就地恢复时自动结束相关软件进程")
    parser.add_argument("--work-dir", help="覆盖 index.json 里记录的工作目录路径(episode 数据被拷到别的机器时需要)")
    args = parser.parse_args()

    episode_dir = Path(args.episode_dir)
    if not episode_dir.exists():
        print(f"[restore] episode 目录不存在: {episode_dir}", file=sys.stderr)
        sys.exit(1)

    index = load_index(episode_dir)

    if args.list:
        list_checkpoints(index)
        return

    work_dir = args.work_dir or index.get("work_dir")
    if not work_dir:
        print("[restore] index.json 里没有 work_dir,请用 --work-dir 指定", file=sys.stderr)
        sys.exit(1)
    work_dir = Path(work_dir)
    git_dir = episode_dir / "snapshots" / "repo.git"
    if not git_dir.exists():
        print(f"[restore] 找不到仓库: {git_dir}", file=sys.stderr)
        sys.exit(1)

    steps = load_trajectory_steps(episode_dir)
    commit = resolve_commit(args, steps)

    r = snapshot.run_git(git_dir, work_dir, ["cat-file", "-e", commit])
    if r.returncode != 0:
        print(f"[restore] commit 不存在: {commit}", file=sys.stderr)
        sys.exit(1)

    if args.in_place:
        work_dir.mkdir(parents=True, exist_ok=True)
        restore_in_place(git_dir, work_dir, commit, args.kill)
    else:
        target = args.export or (episode_dir / "restore" / f"restore_{commit}")
        export_commit(git_dir, work_dir, commit, target)


if __name__ == "__main__":
    main()
