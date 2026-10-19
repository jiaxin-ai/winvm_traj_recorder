"""Checks trajrec_routes.py with a fake recorder (needs Flask; run in this directory:
python -m unittest test_trajrec_routes)."""
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from flask import Flask

import trajrec_routes as routes

# Behaves like main.py as far as the routes can tell: writes raw/events.jsonl, prints the ready line,
# waits for the stop file, writes trajectory.jsonl. "--episode-id fail_*" exits before getting ready.
FAKE_RECORDER = textwrap.dedent('''
    import argparse, os, sys, time
    parser = argparse.ArgumentParser()
    for name in ("--output", "--episode-id", "--task", "--watch", "--stop-file", "--milestone-file"):
        parser.add_argument(name)
    parser.add_argument("--no-watch-gitignore", action="store_true")
    args = parser.parse_args()
    episode = os.path.join(args.output, args.episode_id)
    os.makedirs(os.path.join(episode, "raw"), exist_ok=True)
    if args.episode_id.startswith("fail_"):
        print("[trajrec] 错误: test failure", flush=True)
        sys.exit(1)
    open(os.path.join(episode, "raw", "events.jsonl"), "w").write("{}\\n")
    os.makedirs(os.path.join(episode, "snapshots"), exist_ok=True)
    open(os.path.join(episode, "snapshots", "object"), "w").write("x")
    os.chmod(os.path.join(episode, "snapshots", "object"), 0o444)  # like git's object files
    open(os.path.join(episode, "task.json"), "w", encoding="utf-8").write(open(args.task, encoding="utf-8").read())
    print("[trajrec] 初始状态已记录,可以开始操作", flush=True)
    while not os.path.exists(args.stop_file):
        time.sleep(0.05)
    os.remove(args.stop_file)
    open(os.path.join(episode, "trajectory.jsonl"), "w").write("{}\\n")
''')


class RoutesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.result_root, self.local_root, control = root / "G", root / "episodes", root / "control"
        for path in (self.result_root, self.local_root, control, root / "recorder"):
            path.mkdir()
        (root / "recorder" / "main.py").write_text(FAKE_RECORDER, encoding="utf-8")
        self._saved = {name: getattr(routes, name) for name in
                       ("RESULT_ROOT", "LOCAL_ROOT", "CONTROL_ROOT", "RECORDER_DIR", "RECORDER_PYTHON", "STOP_BUDGET_S", "SYNC_INTERVAL_S", "WATCH_DIR")}
        routes.RESULT_ROOT, routes.LOCAL_ROOT, routes.CONTROL_ROOT = str(self.result_root), str(self.local_root), str(control)
        routes.RECORDER_DIR, routes.RECORDER_PYTHON, routes.STOP_BUDGET_S = str(root / "recorder"), sys.executable, 10
        routes.WATCH_DIR = str(root)
        routes._recordings.clear()
        (self.result_root / "task.json").write_text(json.dumps({"title": "齿轮", "harnessstudio": {"task_title": "齿轮"}}), encoding="utf-8")
        app = Flask("agent")
        app.add_url_rule("/probe", "probe", lambda: "ok")
        self.client = app.test_client()
        self.client.get("/probe")  # an agent service that has already served requests
        self.assertEqual(routes.register(app), "registered on a running app")

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(routes, name, value)
        for entry in list(routes._recordings.values()):
            if entry["process"] is not None and entry["process"].poll() is None:
                entry["process"].kill()
                entry["process"].wait()
        routes._recordings.clear()
        self._tmp.cleanup()

    def start(self, recording_id):
        return self.client.post("/recording/start", json={
            "recording_id": recording_id, "output_dir": os.path.join(str(self.result_root), recording_id),
            "project_id": "p-1", "task_id": "t-1", "vm_id": "vm_1", "chat_id": None})

    def test_a_recording_is_made_locally_and_copied_to_the_result_drive_on_stop(self):
        response = self.start("rec_1")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.client.get("/recording/status").get_json()["recording_id"], "rec_1")
        self.assertFalse((self.result_root / "rec_1").exists(), "nothing goes over the network while recording")

        self.client.post("/execute", headers={"X-HarnessStudio-Operation": "op-7"})  # 404 here, still an Agent request
        self.assertEqual(self.client.post("/recording/milestone", json={"name": "草图完成"}).status_code, 200)
        self.assertEqual((Path(routes.CONTROL_ROOT) / "rec_1" / "MILESTONE").read_text(encoding="utf-8"), "草图完成")

        response = self.client.post("/recording/stop", json={"recording_id": "rec_1"})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["trajectory"], True)
        result = self.result_root / "rec_1"
        for name in ("trajectory.jsonl", "recorder.log", "raw/events.jsonl", "agent_requests.jsonl"):
            self.assertTrue((result / name).exists(), name)
        request = json.loads((result / "agent_requests.jsonl").read_text(encoding="utf-8"))
        self.assertEqual((request["operation"], request["path"], request["status"]), ("op-7", "/execute", 404))
        self.assertLessEqual(request["start_ms"], request["end_ms"])
        task = json.loads((result / "task.json").read_text(encoding="utf-8"))
        self.assertEqual((task["title"], task["harnessstudio"]["task_title"], task["harnessstudio"]["recording_id"]), ("齿轮", "齿轮", "rec_1"))
        self.assertEqual(list(self.local_root.iterdir()), [], "the local copy is removed, read-only files included")
        self.assertFalse((Path(routes.CONTROL_ROOT) / "rec_1").exists())
        self.assertEqual(self.client.post("/recording/stop", json={"recording_id": "rec_1"}).get_json()["message"], "not recording")

    def test_screenshots_are_copied_while_recording_and_files_still_written_are_not(self):
        routes.SYNC_INTERVAL_S = 0.2
        self.assertEqual(self.start("rec_1").status_code, 200)
        (self.local_root / "rec_1" / "screenshots").mkdir()
        (self.local_root / "rec_1" / "screenshots" / "000000.png").write_bytes(b"png")
        (self.local_root / "rec_1" / "recording.mp4").write_bytes(b"partial")
        for _ in range(50):
            if (self.result_root / "rec_1" / "screenshots" / "000000.png").exists():
                break
            time.sleep(0.1)
        self.assertTrue((self.result_root / "rec_1" / "screenshots" / "000000.png").exists())
        self.assertEqual(sorted(path.name for path in (self.result_root / "rec_1").iterdir()), ["screenshots"],
                         "events.jsonl and the video are copied once the recorder has ended")
        self.assertEqual(self.client.post("/recording/stop", json={"recording_id": "rec_1"}).status_code, 200)
        self.assertTrue((self.result_root / "rec_1" / "recording.mp4").exists())
        self.assertTrue((self.result_root / "rec_1" / "raw" / "events.jsonl").exists())

    def test_one_recording_at_a_time_and_requests_are_validated(self):
        self.assertEqual(self.start("rec_1").status_code, 200)
        self.assertEqual(self.start("rec_1").get_json()["message"], "already recording")
        self.assertEqual(self.start("rec_2").status_code, 409)
        bad = self.client.post("/recording/start", json={"recording_id": "rec_3", "output_dir": "C:\\elsewhere"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.client.post("/recording/stop", json={"recording_id": "../x"}).status_code, 400)
        self.assertEqual(self.client.post("/recording/stop", json={"recording_id": "rec_9"}).get_json()["message"], "not recording")

    def test_a_recorder_that_cannot_start_reports_its_log_and_keeps_it(self):
        response = self.start("fail_1")
        self.assertEqual(response.status_code, 500)
        self.assertIn("test failure", response.get_json()["message"])
        for _ in range(50):
            if (self.result_root / "fail_1" / "recorder.log").exists() and not (self.local_root / "fail_1").exists():
                break
            time.sleep(0.1)
        self.assertIn("test failure", (self.result_root / "fail_1" / "recorder.log").read_text(encoding="utf-8"))
        self.assertEqual(self.client.post("/recording/stop", json={"recording_id": "fail_1"}).get_json()["message"], "not recording")

    def test_episodes_left_on_the_local_disk_are_copied_when_the_service_starts(self):
        left = self.local_root / "rec_old"
        (left / "raw").mkdir(parents=True)
        (left / "raw" / "events.jsonl").write_text("{}\n", encoding="utf-8")
        (left / "trajectory.jsonl").write_text("{}\n", encoding="utf-8")
        routes._recover()
        for _ in range(50):
            if not left.exists():
                break
            time.sleep(0.1)
        self.assertTrue((self.result_root / "rec_old" / "trajectory.jsonl").exists())
        self.assertFalse(left.exists())

    def test_a_copied_episode_whose_removal_was_interrupted_is_removed_not_copied_again(self):
        (self.result_root / "rec_done").mkdir()
        (self.result_root / "rec_done" / "trajectory.jsonl").write_text("{}\n", encoding="utf-8")
        left = self.local_root / "rec_done.copied" / "snapshots"
        left.mkdir(parents=True)
        (left / "object").write_text("x", encoding="utf-8")
        (left / "object").chmod(0o444)  # git's object files are read-only
        routes._recover()
        self.assertEqual(list(self.local_root.iterdir()), [])
        self.assertEqual([path.name for path in (self.result_root / "rec_done").iterdir()], ["trajectory.jsonl"])


if __name__ == "__main__":
    unittest.main()
