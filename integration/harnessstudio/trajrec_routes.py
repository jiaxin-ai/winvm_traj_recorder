"""vm_manager / HarnessStudio trajectory-recording routes for the VM agent service.

The agent service is the Flask app in C:\\oem\\server\\main.py (port 5000 in the VM). vm_manager calls

    POST /recording/start  {"recording_id", "output_dir": "G:\\<recording_id>", "project_id", "task_id", "vm_id", "chat_id"}
    POST /recording/stop   {"recording_id"}

when a VM starts running and before it is stopped or deleted, and expects HTTP 200. These routes run the
Windows Trajectory Recorder (main.py of this repository, installed in C:\\trajrec\\recorder) as a child process in the
agent service's desktop session, so its keyboard/mouse hooks and screenshots see the user's desktop:

    python main.py --output C:\\trajrec\\episodes --episode-id <recording_id> --task <task.json> --watch D:\\
                   --no-watch-gitignore --stop-file / --milestone-file C:\\ProgramData\\trajrec\\<recording_id>\\STOP / MILESTONE

The recorder writes to the local disk: G: is a network drive (SMB) on which one append takes 10-50 ms, and the
recorder appends every input event from inside its low-level keyboard/mouse hooks, so recording to G: directly
delays the desktop's input and loses events. While recording, the screenshots (most of the data) are copied to
G:\\<recording_id> (vm_manager's result directory) once a minute; once the recorder has ended the rest is copied
and the local copy is removed. An episode that could not be finished or copied (the VM was shut down) is finished
and copied the next time the agent service starts.

Also provided for HarnessStudio:

    POST /recording/milestone {"name", "recording_id"?}   named file snapshot in the running recording
    GET  /recording/status                               the running recording, if any

and every request carrying an ``X-HarnessStudio-Operation`` header (HarnessStudio's Agent tool calls) is
appended to <episode>\\agent_requests.jsonl with start/end times on the VM clock, so each trajectory step
can be attributed to the Agent operation that caused it (or to the human).

Install: copy this file next to main.py and add, before ``if __name__ == '__main__':``

    import trajrec_routes
    trajrec_routes.register(app)

``register`` can also be called on an already serving app (e.g. through /execute_windows) without a
restart; see README.md in this directory.
"""
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import threading
import time

from flask import jsonify, request
from werkzeug.routing import Rule

# Layout made by install_recorder.ps1: C:\trajrec\recorder (this repository) and C:\trajrec\venv.
INSTALL_DIR = os.environ.get("TRAJREC_HOME", r"C:\trajrec")
RECORDER_DIR = os.path.join(INSTALL_DIR, "recorder")
RECORDER_PYTHON = os.path.join(INSTALL_DIR, "venv", "Scripts", "python.exe")
LOCAL_ROOT = os.path.join(INSTALL_DIR, "episodes")  # --output: episodes are recorded on the local disk
FFMPEG_DIR = os.environ.get("TRAJREC_FFMPEG_DIR", r"C:\ffmpeg\bin")  # the image's ffmpeg; added to PATH for screen.py
WATCH_DIR = os.environ.get("TRAJREC_WATCH", "D:\\")
RESULT_ROOT = "G:\\"  # vm_manager's recordings share: finished episodes are copied to G:\<recording_id>
CONTROL_ROOT = r"C:\ProgramData\trajrec"
READY_TIMEOUT_S = 40
# How long /recording/stop waits for the episode to be finished and copied. Keep it below vm_manager's
# recorder_timeout_seconds (60 by default); whatever is left continues in the background.
STOP_BUDGET_S = float(os.environ.get("TRAJREC_STOP_BUDGET", "50"))
FINISH_TIMEOUT_S = 900  # a recorder still running this long after STOP is killed
REGENERATE_TIMEOUT_S = 600
COPY_TIMEOUT_S = 1800
SYNC_INTERVAL_S = 60
# D: and G: are network drives that Windows reconnects on first use after logon, which is about when
# vm_manager starts the first recording.
DRIVE_WAIT_AT_START_S = 20
DRIVE_WAIT_AT_FINISH_S = 600
COPY_ATTEMPTS = 3
READY_TEXT = "初始状态已记录"
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
OPERATION_HEADER = "X-HarnessStudio-Operation"

log = logging.getLogger("trajrec_routes")
_lock = threading.Lock()
# recording_id -> {"process", "episode" (local), "control", "log", "finished": Event, "result": dict | None}
_recordings = {}


def _now_ms():
    return int(time.time() * 1000)  # the recorder's own clock (input_recorder.now_ms)


def _control(recording_id):
    return os.path.join(CONTROL_ROOT, recording_id)


def _local(recording_id):
    return os.path.join(LOCAL_ROOT, recording_id)


def _result(recording_id):
    return os.path.join(RESULT_ROOT, recording_id)


def _running():
    """The recording whose recorder process is alive, as (recording_id, entry), or (None, None)."""
    for recording_id, entry in _recordings.items():
        if entry["process"] is not None and entry["process"].poll() is None:
            return recording_id, entry
    return None, None


def _tail(path, limit=4000):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _entry(recording_id, process):
    return {"process": process, "episode": _local(recording_id), "control": _control(recording_id),
            "log": os.path.join(_local(recording_id), "recorder.log"), "finished": threading.Event(), "result": None,
            "copy_lock": threading.Lock()}


class _Adopted:
    """A recorder started by an earlier agent service process (Flask's reloader restarts it), behind the
    small part of the Popen interface used here."""

    def __init__(self, process):
        self._process = process
        self.pid = process.pid
        self.returncode = None

    def poll(self):
        try:
            if self._process.is_running() and self._process.status() != "zombie":
                return None
        except Exception:
            pass
        if self.returncode is None:
            self.returncode = -1
        return self.returncode

    def wait(self, timeout=None):
        import psutil
        try:
            self.returncode = self._process.wait(timeout) or 0
        except psutil.TimeoutExpired:
            raise subprocess.TimeoutExpired("recorder", timeout)
        return self.returncode

    def kill(self):
        self._process.kill()


def _adopt(recording_id):
    """The live recorder of an episode, from the pid file written at start, or None."""
    try:
        import psutil
        with open(os.path.join(_control(recording_id), "recorder.pid"), encoding="utf-8") as handle:
            process = psutil.Process(int(handle.read().strip()))
        if process.is_running() and recording_id in " ".join(process.cmdline()):
            return _Adopted(process)
    except Exception:  # no pid file, process gone, no psutil, no access
        pass
    return None


def _wait_for(path, timeout):
    """True once path is reachable; every failed look also asks Windows to reconnect the drive."""
    deadline = time.time() + timeout
    while not os.path.isdir(path):
        if time.time() >= deadline:
            return False
        time.sleep(1)
    return True


def _task_file(recording_id, body):
    """task.json for --task: HarnessStudio writes G:\\task.json (task title, goal, checklist, IDs) when it
    creates the VM; vm_manager's IDs are added. Kept outside --watch and outside the episode."""
    task = {}
    try:
        with open(os.path.join(RESULT_ROOT, "task.json"), encoding="utf-8") as handle:
            task = json.load(handle)
    except (OSError, ValueError):
        pass
    if not isinstance(task, dict):
        task = {}
    task.setdefault("title", "HarnessStudio 任务 %s" % body.get("task_id", ""))
    task.setdefault("task_instruction", "")
    ids = {key: body.get(key) for key in ("recording_id", "project_id", "task_id", "vm_id", "chat_id")}
    task["harnessstudio"] = {**(task.get("harnessstudio") or {}), **ids}
    path = os.path.join(_control(recording_id), "task.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(task, handle, ensure_ascii=False, indent=2)
    return path


def _copy_tree(source, target):
    """Copies new and changed files. Never removes anything from target: what is on G: is the result."""
    if os.name != "nt":  # tests; like robocopy, files whose size and time are unchanged are skipped
        def copy_changed(src, dst):
            if os.path.exists(dst) and (os.stat(src).st_size, int(os.stat(src).st_mtime)) == (os.stat(dst).st_size, int(os.stat(dst).st_mtime)):
                return dst
            return shutil.copy2(src, dst)
        shutil.copytree(source, target, dirs_exist_ok=True, copy_function=copy_changed)
        return
    # /A-:R: git's object files are read-only; their copies are not, so a later copy can always replace them.
    command = ["robocopy", source, target, "/E", "/A-:R", "/R:2", "/W:1", "/NP", "/NFL", "/NDL", "/NJH", "/NJS"]
    copy = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=COPY_TIMEOUT_S)
    if copy.returncode >= 8:
        raise RuntimeError("robocopy exited with %s: %s" % (copy.returncode, copy.stdout[-800:].decode("utf-8", "replace")))


def _remove_tree(path):
    """Removes a local episode, including git's read-only object files."""
    def writable_and_retry(function, target, _exc_info):
        os.chmod(target, stat.S_IWRITE)
        function(target)
    if not os.path.exists(path):
        return
    try:
        shutil.rmtree(path, onerror=writable_and_retry)
    except OSError as exc:
        log.warning("cannot remove %s: %s", path, exc)


def _finish(recording_id, entry):
    """Waits for the recorder to end, makes sure trajectory.jsonl exists, copies the episode to G: and removes
    the local copy. Runs in its own thread; sets entry["finished"]."""
    local, result = entry["episode"], {"recording_id": recording_id, "episode": _result(recording_id)}
    try:
        process = entry["process"]
        if process is not None:
            try:
                result["returncode"] = process.wait(timeout=FINISH_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                process.kill()
                result["returncode"] = "killed"
        if os.path.isdir(local):
            if not _wait_for(RESULT_ROOT, DRIVE_WAIT_AT_FINISH_S):
                raise RuntimeError("%s is not reachable" % RESULT_ROOT)
            if not os.path.exists(os.path.join(local, "trajectory.jsonl")) and os.path.exists(os.path.join(local, "raw", "events.jsonl")):
                # The recorder did not end normally (killed, VM shut down): rebuild the trajectory from raw/.
                with open(os.path.join(local, "recorder.log"), "ab") as out:
                    subprocess.run([RECORDER_PYTHON, os.path.join(RECORDER_DIR, "trajectory.py"), local], cwd=RECORDER_DIR,
                                   stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, timeout=REGENERATE_TIMEOUT_S)
            with entry["copy_lock"]:
                for attempt in range(COPY_ATTEMPTS):
                    try:
                        _copy_tree(local, _result(recording_id))
                        break
                    except Exception:
                        if attempt == COPY_ATTEMPTS - 1:
                            raise
                        time.sleep(10)
                # Renamed before it is removed: a partly removed episode must never be taken for one still to
                # be copied (_recover skips and removes these).
                copied = local + ".copied"
                os.replace(local, copied)
                _remove_tree(copied)
        _remove_tree(entry["control"])
        result["trajectory"] = os.path.exists(os.path.join(_result(recording_id), "trajectory.jsonl"))
        result["status"] = "success"
        log.info("recording %s finished: %s", recording_id, result)
    except Exception as exc:  # the local copy stays and is retried when the agent service starts again
        result.update(status="error", message=str(exc))
        log.warning("recording %s could not be finished: %s", recording_id, exc)
    entry["result"] = result
    entry["finished"].set()
    with _lock:
        if _recordings.get(recording_id) is entry:
            del _recordings[recording_id]


def _sync(recording_id, entry):
    """While recording, copies the screenshots to G: once a minute, so that stop only copies the rest.
    Only the screenshots: each is written once and never opened again, while the recorder appends to
    raw/events.jsonl and its logs from inside its input hooks, and robocopy reading a file makes the
    recorder's next append fail with "Permission denied", which stops pynput's listener for good."""
    while not entry["finished"].wait(SYNC_INTERVAL_S):
        with entry["copy_lock"]:
            if entry.get("finisher") or entry["process"] is None or entry["process"].poll() is not None:
                return
            try:
                screenshots = os.path.join(entry["episode"], "screenshots")
                if os.path.isdir(screenshots):
                    _copy_tree(screenshots, os.path.join(_result(recording_id), "screenshots"))
            except Exception as exc:
                log.info("recording %s: periodic copy failed: %s", recording_id, exc)


def _start_finisher(recording_id, entry):
    with _lock:
        if entry.get("finisher"):
            return
        entry["finisher"] = threading.Thread(target=_finish, args=(recording_id, entry), name="trajrec-finish-" + recording_id, daemon=True)
    entry["finisher"].start()


def _recover():
    """At agent service start: adopts recorders still running from before a service restart, and finishes
    episodes left on the local disk (stop not called, or the VM was shut down before the copy)."""
    try:
        names = sorted(os.listdir(LOCAL_ROOT))
    except OSError:
        return
    for recording_id in names:
        if recording_id.endswith(".copied"):
            _remove_tree(os.path.join(LOCAL_ROOT, recording_id))
            continue
        if not IDENTIFIER.match(recording_id):
            continue
        process = _adopt(recording_id)
        entry = _entry(recording_id, process)
        with _lock:
            if recording_id in _recordings:
                continue
            _recordings[recording_id] = entry
        if process is None:
            log.info("recording %s: finishing an episode left by an earlier run", recording_id)
            _start_finisher(recording_id, entry)
        else:
            log.info("recording %s: adopted running recorder %s", recording_id, process.pid)
            threading.Thread(target=_sync, args=(recording_id, entry), name="trajrec-sync-" + recording_id, daemon=True).start()


def recording_start():
    body = request.get_json(silent=True) or {}
    recording_id, output_dir = str(body.get("recording_id", "")), str(body.get("output_dir", ""))
    if not IDENTIFIER.match(recording_id) or output_dir.rstrip("\\") != _result(recording_id):
        return jsonify({"status": "error", "message": "invalid recording_id or output_dir"}), 400
    with _lock:
        running_id, running = _running()
        if running_id == recording_id:
            return jsonify({"status": "success", "message": "already recording", "episode": running["episode"]})
        if running_id is not None:
            # The recorder's hooks are global: one recording per VM.
            return jsonify({"status": "error", "message": "recording %s is still running" % running_id}), 409
        if recording_id in _recordings:
            return jsonify({"status": "error", "message": "recording %s has already ended" % recording_id}), 409
        entry = _entry(recording_id, None)
        for drive in (RESULT_ROOT, WATCH_DIR):  # G:\task.json; the recorder's --watch
            if not _wait_for(drive, DRIVE_WAIT_AT_START_S):
                log.warning("recording %s: %s is not reachable", recording_id, drive)
        os.makedirs(entry["control"], exist_ok=True)
        os.makedirs(entry["episode"], exist_ok=True)
        for name in ("STOP", "MILESTONE"):  # stale control files of an earlier attempt
            try:
                os.remove(os.path.join(entry["control"], name))
            except OSError:
                pass
        task = _task_file(recording_id, body)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        env["PATH"] = FFMPEG_DIR + os.pathsep + env.get("PATH", "")
        command = [RECORDER_PYTHON, "-u", os.path.join(RECORDER_DIR, "main.py"),
                   "--output", LOCAL_ROOT, "--episode-id", recording_id, "--task", task, "--watch", WATCH_DIR,
                   "--stop-file", os.path.join(entry["control"], "STOP"),
                   "--milestone-file", os.path.join(entry["control"], "MILESTONE"),
                   "--no-watch-gitignore"]  # D:\ is the task's deliverable; keep the recorder's files out of it
        with open(entry["log"], "ab") as out:
            process = subprocess.Popen(command, cwd=RECORDER_DIR, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                       stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        with open(os.path.join(entry["control"], "recorder.pid"), "w", encoding="utf-8") as handle:
            handle.write(str(process.pid))
        entry["process"] = process
        _recordings[recording_id] = entry
    threading.Thread(target=_sync, args=(recording_id, entry), name="trajrec-sync-" + recording_id, daemon=True).start()
    deadline = time.time() + READY_TIMEOUT_S
    while time.time() < deadline:
        if process.poll() is not None:
            message = "recorder exited with %s: %s" % (process.returncode, _tail(entry["log"], 1500))
            _start_finisher(recording_id, entry)  # what it wrote, recorder.log included, still goes to G:
            return jsonify({"status": "error", "message": message}), 500
        if READY_TEXT in _tail(entry["log"], 16000):
            return jsonify({"status": "success", "episode": entry["episode"], "pid": process.pid})
        time.sleep(0.5)
    # Still starting (slow first UIA/screenshot); it keeps running and becomes ready on its own.
    return jsonify({"status": "success", "message": "recorder started; initial observation still pending",
                    "episode": entry["episode"], "pid": process.pid})


def recording_stop():
    body = request.get_json(silent=True) or {}
    recording_id = str(body.get("recording_id", ""))
    if not IDENTIFIER.match(recording_id):
        return jsonify({"status": "error", "message": "invalid recording_id"}), 400
    with _lock:
        entry = _recordings.get(recording_id)
    control = _control(recording_id)
    if entry is None:
        if not os.path.isdir(control):
            # Idempotent: vm_manager also calls stop to clean up after a failed start.
            return jsonify({"status": "success", "message": "not recording"})
        # A recorder this service does not know about: the stop file still reaches it.
        with open(os.path.join(control, "STOP"), "w", encoding="utf-8"):
            pass
        return jsonify({"status": "success", "message": "stop requested"})
    if entry["process"] is not None and entry["process"].poll() is None:
        with open(os.path.join(control, "STOP"), "w", encoding="utf-8"):
            pass
    _start_finisher(recording_id, entry)
    if not entry["finished"].wait(STOP_BUDGET_S):
        return jsonify({"status": "success", "message": "stopping; the episode is still being finished and copied to %s" % _result(recording_id)})
    return jsonify(entry["result"]), 200 if entry["result"]["status"] == "success" else 500


def recording_milestone():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()[:200]
    with _lock:
        running_id, running = _running()
    recording_id = str(body.get("recording_id") or running_id or "")
    if running_id is None or recording_id != running_id:
        return jsonify({"status": "error", "message": "no running recording"}), 409
    with open(os.path.join(running["control"], "MILESTONE"), "w", encoding="utf-8") as handle:
        handle.write(name)
    return jsonify({"status": "success", "recording_id": running_id, "name": name})


def recording_status():
    with _lock:
        running_id, running = _running()
        finishing = sorted(key for key in _recordings if key != running_id)
    return jsonify({"status": "success", "recording_id": running_id, "finishing": finishing,
                    "episode": running["episode"] if running else None, "pid": running["process"].pid if running else None})


def _operation_started():
    operation = request.headers.get(OPERATION_HEADER, "")
    if IDENTIFIER.match(operation):
        request.environ["harnessstudio.operation"] = (operation, _now_ms())


def _operation_finished(response):
    started = request.environ.get("harnessstudio.operation")
    if started:
        with _lock:
            running_id, running = _running()
        if running is not None:
            line = {"operation": started[0], "method": request.method, "path": request.path,
                    "start_ms": started[1], "end_ms": _now_ms(), "status": response.status_code}
            try:
                with open(os.path.join(running["episode"], "agent_requests.jsonl"), "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(line, ensure_ascii=False) + "\n")
            except OSError:
                pass  # attribution is best effort; never fail the Agent's request
    return response


_ROUTES = [
    ("/recording/start", "trajrec_recording_start", recording_start, ["POST"]),
    ("/recording/stop", "trajrec_recording_stop", recording_stop, ["POST"]),
    ("/recording/milestone", "trajrec_recording_milestone", recording_milestone, ["POST"]),
    ("/recording/status", "trajrec_recording_status", recording_status, ["GET"]),
]


def register(app):
    """Adds the routes and the operation hooks, and finishes episodes left from an earlier run. On an app
    that has already served requests (Flask refuses add_url_rule then), the rule map and hook lists are
    updated directly, so no restart is needed."""
    if "trajrec_recording_start" in app.view_functions:
        return "already registered"
    try:
        for path, endpoint, view, methods in _ROUTES:
            app.add_url_rule(path, endpoint, view, methods=methods)
        app.before_request(_operation_started)
        app.after_request(_operation_finished)
        result = "registered"
    except AssertionError:
        for path, endpoint, view, methods in _ROUTES:
            app.url_map.add(Rule(path, endpoint=endpoint, methods=methods))
            app.view_functions[endpoint] = view
        app.before_request_funcs.setdefault(None, []).append(_operation_started)
        app.after_request_funcs.setdefault(None, []).append(_operation_finished)
        result = "registered on a running app"
    threading.Thread(target=_recover, name="trajrec-recover", daemon=True).start()
    return result
