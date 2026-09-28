#!/usr/bin/env python3
"""File snapshotting for one recording episode: a local, per-episode bare
git repo that commits the watched work directory on file changes and on
milestones, so every step of the trajectory can be tied to a concrete file
version. See task-v1.1.md and README.md for the full design.

git itself is cross-platform, but this module is only ever driven by
main.py's Windows-only Episode, and its own standalone test below assumes
nothing Windows-specific, so it runs on macOS/Linux for development too.

Repo layout (never touches the user's own git, never writes into the
watched directory except for .gitignore):
    <episode_dir>/snapshots/repo.git/   GIT_DIR (bare)
    <watch_dir>/                        GIT_WORK_TREE

Every git invocation goes through run_git() below, which sets GIT_DIR and
GIT_WORK_TREE. Nothing else in this file (or restore.py) shells out to git
directly.

Standalone test (works on macOS too):
    python snapshot.py --watch /tmp/watch --episode-dir /tmp/ep1
Then edit files under /tmp/watch in another window (commits appear after
5s of quiet), or press Enter in this terminal to fire a milestone, or
Ctrl+C to finalize (writes repo.bundle) and exit.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

GITIGNORE_PATTERNS = [
    "~$*", "*.tmp", "*.bak", "*.dwl", "*.dwl2", "*.swp.*", "*.err", "*.ac$", "*.sv$", "_discarded/",
]

MILESTONE_FILE = Path(r"C:\ProgramData\trajrec\MILESTONE")
MILESTONE_POLL_INTERVAL_S = 0.5

QUIET_PERIOD_MS = 3000       # commit only after this long with no file events
QUIET_TIMEOUT_MS = 30000     # ...but never wait longer than this
QUIET_POLL_INTERVAL_S = 0.2

LARGE_FILE_WARN_BYTES = 200 * 1024 * 1024
GIT_TIMEOUT_S = 30


def now_ms():
    return int(time.time() * 1000)


def run_git(git_dir, work_tree, args, timeout=GIT_TIMEOUT_S, set_work_tree=True, text=True):
    """The one place that shells out to git -- used by both this module and
    restore.py, so nothing else ever assembles a git command line. Sets
    GIT_DIR/GIT_WORK_TREE so every call is scoped to this episode's bare
    repo and this run's work tree, regardless of the caller's own cwd or
    any git repo the user might have lying around. Returns the
    CompletedProcess; callers check returncode themselves.

    set_work_tree=False exists for exactly one caller: `git init --bare`
    refuses to run at all if GIT_WORK_TREE is set ("not allowed without
    specifying GIT_DIR", even though GIT_DIR *is* set -- a bare repo isn't
    allowed to have a work tree by definition). Every other call needs it.

    text=False exists for `git archive`, whose stdout is a binary tar
    stream that must not be decoded.

    git_dir/work_tree are resolved to absolute paths before use: this
    call also sets the subprocess's own cwd to work_tree, so a *relative*
    GIT_DIR/GIT_WORK_TREE would get re-resolved by git against that new
    cwd instead of the caller's original one, silently producing a wrong
    (sometimes doubled-looking) path."""
    git_dir = Path(git_dir).resolve()
    work_tree = Path(work_tree).resolve()
    env = dict(os.environ)
    env["GIT_DIR"] = str(git_dir)
    if set_work_tree:
        env["GIT_WORK_TREE"] = str(work_tree)
    else:
        env.pop("GIT_WORK_TREE", None)
    return subprocess.run(
        ["git", *args], cwd=str(work_tree), env=env,
        capture_output=True, text=text, timeout=timeout,
    )


def check_git_available():
    try:
        r = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _write_gitignore(watch_dir):
    """Adds the fixed set of noise patterns to <watch_dir>/.gitignore,
    appending only whatever isn't already there. Must live in the work
    tree, not the bare repo -- git reads .gitignore from GIT_WORK_TREE."""
    path = Path(watch_dir) / ".gitignore"
    existing_lines = []
    if path.exists():
        existing_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    missing = [p for p in GITIGNORE_PATTERNS if p not in existing_lines]
    if not missing:
        return
    with path.open("a", encoding="utf-8") as f:
        if existing_lines and existing_lines[-1] != "":
            f.write("\n")
        for p in missing:
            f.write(p + "\n")


class SnapshotManager:
    """Owns the per-episode git repo and the single background thread that
    performs every commit. Everything else -- the file-watcher callback,
    the MILESTONE control-file poller, the Ctrl+Alt+Shift+M hotkey -- only
    touches a small piece of lock-protected state via notify_*()/finalize()
    and never blocks on git I/O itself.

    Concurrency rules (task-v1.1.md "并发处理"): only one snapshot runs at
    a time (including its quiet-wait). A file_changed trigger that arrives
    while busy is dropped (the in-flight snapshot will already capture the
    latest state). A milestone or episode_end trigger that arrives while
    busy is queued -- at most one, multiple collapse into the latest name --
    and runs immediately after the current one finishes.
    """

    def __init__(self, episode_dir, watch_dir, writer, enabled=True):
        # Resolved to absolute right away: run_git() sets the git
        # subprocess's cwd to watch_dir, so any *relative* path built from
        # these later (e.g. finalize()'s repo.bundle path, passed as a
        # plain git argument rather than an env var) would otherwise get
        # silently re-resolved by git against that different cwd instead
        # of the caller's original one.
        self.episode_dir = Path(episode_dir).resolve()
        self.watch_dir = Path(watch_dir).resolve()
        self.writer = writer
        self.requested_enabled = enabled
        self.enabled = False
        self.disabled_reason = None
        self.git_dir = self.episode_dir / "snapshots" / "repo.git"

        self._file_observer = None
        self._cv = threading.Condition()
        self._pending = None       # next trigger dict to run, or None
        self._queued_next = None   # a non-droppable trigger queued while busy
        self._busy = False
        self._stop = False
        self._worker_thread = None
        self._milestone_thread = None

    # -- setup -------------------------------------------------------------
    def init_repo(self):
        """Creates the bare repo and does the episode_start baseline setup.
        Returns the effective enabled flag: False (with disabled_reason
        set) if snapshots were turned off, git is missing, or init failed
        for any reason -- in which case the caller keeps recording without
        snapshots, per task-v1.1.md."""
        if not self.requested_enabled:
            self.disabled_reason = "disabled by --no-snapshot"
            return False
        if not check_git_available():
            self.disabled_reason = "git not found"
            print("[snapshot] git 不可用,关闭快照功能,继续录制", file=sys.stderr)
            return False
        try:
            self.git_dir.mkdir(parents=True, exist_ok=True)
            self.watch_dir.mkdir(parents=True, exist_ok=True)
            r = run_git(self.git_dir, self.watch_dir, ["init", "--bare"], set_work_tree=False)
            if r.returncode != 0:
                raise RuntimeError(r.stderr.strip())
            for key, value in (
                ("core.autocrlf", "false"),
                ("user.name", "trajrec"),
                ("user.email", "trajrec@local"),
                ("gc.auto", "0"),
            ):
                run_git(self.git_dir, self.watch_dir, ["config", key, value])
            _write_gitignore(self.watch_dir)
        except Exception as exc:
            self.disabled_reason = f"init failed: {exc}"
            print(f"[snapshot] 初始化仓库失败,关闭快照功能,继续录制: {exc}", file=sys.stderr)
            return False
        self.enabled = True
        self.disabled_reason = None
        return True

    def start(self, file_observer):
        """Starts the worker + milestone-poller threads and blocks until
        the episode_start baseline commit is done."""
        if not self.enabled:
            return
        self._file_observer = file_observer
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        self._milestone_thread = threading.Thread(target=self._milestone_file_poll_loop, daemon=True)
        self._milestone_thread.start()
        ev = self._submit("episode_start", None, now_ms(), droppable=False)
        if ev is not None:
            ev.wait(timeout=GIT_TIMEOUT_S + QUIET_TIMEOUT_MS / 1000 + 5)

    # -- triggers ------------------------------------------------------------
    def notify_file_changed(self, t_ms):
        """Leading-edge hook: call this for every raw file_event. Cheap and
        non-blocking -- only touches the small pending/busy state."""
        if not self.enabled:
            return
        self._submit("file_changed", None, t_ms, droppable=True)

    def notify_milestone(self, name, t_ms):
        if not self.enabled:
            return
        self._submit("milestone", name, t_ms, droppable=False)

    def finalize(self):
        """Commits episode_end, stops the worker thread, and bundles the
        repo. Blocking -- call this from Episode.stop() after the file
        observer has already been joined."""
        if not self.enabled:
            return
        ev = self._submit("episode_end", None, now_ms(), droppable=False)
        if ev is not None:
            ev.wait(timeout=GIT_TIMEOUT_S + QUIET_TIMEOUT_MS / 1000 + 5)
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        if self._worker_thread:
            self._worker_thread.join(timeout=5)
        if self._milestone_thread:
            self._milestone_thread.join(timeout=2)
        try:
            bundle_path = self.episode_dir / "snapshots" / "repo.bundle"
            r = run_git(self.git_dir, self.watch_dir, ["bundle", "create", str(bundle_path), "--all"])
            if r.returncode != 0:
                print(f"[snapshot] 打包 repo.bundle 失败: {r.stderr.strip()}", file=sys.stderr)
        except Exception as exc:
            print(f"[snapshot] 打包 repo.bundle 失败: {exc}", file=sys.stderr)

    # -- internal: trigger queue --------------------------------------------
    def _submit(self, reason, name, requested_t_ms, droppable):
        with self._cv:
            if self._busy:
                if droppable:
                    return None
                ev = threading.Event()
                self._queued_next = {"reason": reason, "name": name,
                                      "requested_t_ms": requested_t_ms, "event": ev}
                return ev
            if self._pending is not None:
                if droppable:
                    return None
                # Collapse into whatever is already waiting to run next --
                # "队列中最多保留一个 milestone,多个合并为一个,名称取最后一个".
                self._pending["event"].set()
                ev = threading.Event()
                self._pending = {"reason": reason, "name": name,
                                  "requested_t_ms": requested_t_ms, "event": ev}
                return ev
            ev = threading.Event()
            self._pending = {"reason": reason, "name": name,
                              "requested_t_ms": requested_t_ms, "event": ev}
            self._cv.notify()
            return ev

    def _worker_loop(self):
        while True:
            with self._cv:
                while self._pending is None and not self._stop:
                    self._cv.wait()
                if self._pending is None:
                    return
                trigger = self._pending
                self._pending = None
                self._busy = True
            self._execute(trigger)
            with self._cv:
                self._busy = False
                if self._queued_next is not None:
                    self._pending = self._queued_next
                    self._queued_next = None
                    self._cv.notify()

    # -- internal: doing the actual commit -----------------------------------
    def _quiet_wait(self):
        """Waits until the watched directory has been quiet for
        QUIET_PERIOD_MS, capped at QUIET_TIMEOUT_MS. Uses the last_event_ms
        attribute files.py's start_watch() exposes on its Observer."""
        start = now_ms()
        while True:
            elapsed_total = now_ms() - start
            if elapsed_total >= QUIET_TIMEOUT_MS:
                return elapsed_total, True
            last = getattr(self._file_observer, "last_event_ms", None) if self._file_observer else None
            if last is None or (now_ms() - last) >= QUIET_PERIOD_MS:
                return elapsed_total, False
            time.sleep(QUIET_POLL_INTERVAL_S)

    def _execute(self, trigger):
        reason = trigger["reason"]
        name = trigger["name"]
        requested_t_ms = trigger["requested_t_ms"]
        waited_ms, quiet_timeout = self._quiet_wait()

        try:
            run_git(self.git_dir, self.watch_dir, ["add", "-A"])
            msg = reason
            if name:
                msg += f" {name}"
            msg += f" t={requested_t_ms}"
            r = run_git(self.git_dir, self.watch_dir, ["commit", "--allow-empty", "-m", msg])
            if r.returncode != 0:
                raise RuntimeError(r.stderr.strip())

            r = run_git(self.git_dir, self.watch_dir, ["rev-parse", "HEAD"])
            if r.returncode != 0:
                raise RuntimeError(r.stderr.strip())
            commit = r.stdout.strip()[:12]

            count_r = run_git(self.git_dir, self.watch_dir, ["rev-list", "--count", "HEAD"])
            is_first_commit = count_r.stdout.strip() == "1"
            if is_first_commit:
                # HEAD~1 doesn't exist yet; list everything in this commit
                # instead (task-v1.1.md doesn't cover this edge case).
                # --root is required or diff-tree reports nothing at all
                # for a root commit.
                diff_r = run_git(self.git_dir, self.watch_dir,
                                  ["diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "HEAD"])
            else:
                diff_r = run_git(self.git_dir, self.watch_dir, ["diff", "--name-only", "HEAD~1", "HEAD"])
            files_changed = [line for line in diff_r.stdout.splitlines() if line]
        except Exception as exc:
            print(f"[snapshot] 快照提交失败,继续录制: {exc}", file=sys.stderr)
            self.writer.write({
                "t_ms": now_ms(), "type": "snapshot_error", "reason": reason,
                "requested_t_ms": requested_t_ms, "error": str(exc),
            })
            trigger["event"].set()
            return

        committed_t_ms = now_ms()
        for rel_path in files_changed:
            abs_path = self.watch_dir / rel_path
            try:
                if abs_path.is_file() and abs_path.stat().st_size > LARGE_FILE_WARN_BYTES:
                    self.writer.write({
                        "t_ms": now_ms(), "type": "snapshot_large_file",
                        "commit": commit, "path": rel_path, "size": abs_path.stat().st_size,
                    })
            except OSError:
                pass

        self.writer.write({
            "t_ms": committed_t_ms, "type": "snapshot",
            "requested_t_ms": requested_t_ms, "committed_t_ms": committed_t_ms,
            "commit": commit, "reason": reason, "name": name,
            "files_changed": files_changed, "waited_ms": waited_ms, "quiet_timeout": quiet_timeout,
        })
        trigger["event"].set()

    # -- internal: milestone control file ------------------------------------
    def _milestone_file_poll_loop(self):
        while not self._stop:
            try:
                if MILESTONE_FILE.exists():
                    try:
                        content = MILESTONE_FILE.read_text(encoding="utf-8", errors="replace").strip()
                    except OSError:
                        content = ""
                    try:
                        MILESTONE_FILE.unlink()
                    except OSError:
                        pass
                    self.notify_milestone(content if content else None, now_ms())
            except Exception as exc:
                print(f"[snapshot] milestone 文件检查出错: {exc}", file=sys.stderr)
            time.sleep(MILESTONE_POLL_INTERVAL_S)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", required=True, help="工作目录")
    parser.add_argument("--episode-dir", required=True, help="episode 目录(用于放 snapshots/)")
    args = parser.parse_args()

    class _JsonlWriter:
        """Same on-disk format main.py's real writer uses, so this test
        run's raw/events.jsonl can be fed straight into `python
        trajectory.py <episode-dir>` and then `restore.py` afterward --
        the whole chain is testable without running the full recorder."""

        def __init__(self, path):
            self._path = Path(path)
            self._path.parent.mkdir(parents=True, exist_ok=True)

        def write(self, event):
            print(json.dumps(event, ensure_ascii=False))
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

    class _FakeObserver:
        last_event_ms = None

    watch_dir = Path(args.watch).resolve()
    episode_dir = Path(args.episode_dir).resolve()
    watch_dir.mkdir(parents=True, exist_ok=True)

    writer = _JsonlWriter(episode_dir / "raw" / "events.jsonl")
    mgr = SnapshotManager(episode_dir, watch_dir, writer, enabled=True)
    if not mgr.init_repo():
        print(f"[snapshot] 未启用: {mgr.disabled_reason}")
        return

    import files
    if sys.platform == "win32":
        observer = files.start_watch(watch_dir, lambda e: mgr.notify_file_changed(now_ms()))
    else:
        observer = _FakeObserver()
        print("[snapshot] 非 Windows 平台,跳过真实文件监控,只能用回车测试 milestone")
    mgr.start(observer)
    print(f"[snapshot] 已启动,仓库: {mgr.git_dir}。回车触发 milestone,Ctrl+C 结束。")
    try:
        while True:
            input()
            mgr.notify_milestone("manual_test", now_ms())
    except KeyboardInterrupt:
        pass
    finally:
        if sys.platform == "win32":
            observer.stop()
            observer.join()
        mgr.finalize()
        meta = {"watch_dir": str(watch_dir), "snapshot": {"enabled": mgr.enabled, "reason": mgr.disabled_reason}}
        (episode_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[snapshot] 已结束,repo.bundle 已生成。可以运行:\n"
              f"    python trajectory.py {episode_dir}\n"
              f"    python restore.py {episode_dir / 'snapshots'} --list")


if __name__ == "__main__":
    main()
