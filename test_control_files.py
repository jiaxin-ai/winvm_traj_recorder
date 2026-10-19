"""Cross-platform checks for --episode-id / --stop-file / --milestone-file / --no-watch-gitignore
(run: python -m unittest test_control_files)."""
import tempfile
import threading
import time
import unittest
from pathlib import Path

import main
import snapshot


class ControlFileTests(unittest.TestCase):
    def test_episode_id_names_the_episode_directory(self):
        with tempfile.TemporaryDirectory() as root:
            episode = main.make_episode_dir(root, "rec_1234abcd")
            self.assertEqual(episode, Path(root) / "rec_1234abcd")
            for sub in ("screenshots", "artifacts", "raw", "raw/terminal"):
                self.assertTrue((episode / sub).is_dir())

    def test_an_existing_recording_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            episode = main.make_episode_dir(root, "rec_1")
            (episode / "raw" / "events.jsonl").write_text("{}\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                main.make_episode_dir(root, "rec_1")

    def test_episode_id_pattern(self):
        self.assertTrue(main.EPISODE_ID_PATTERN.match("rec_d867647c"))
        for bad in ("../x", "a b", "", "x" * 65, "C:\\x"):
            self.assertIsNone(main.EPISODE_ID_PATTERN.match(bad), bad)

    def test_custom_stop_file_ends_the_wait(self):
        with tempfile.TemporaryDirectory() as root:
            stop = Path(root) / "rec_1" / "STOP"
            waiter = threading.Thread(target=main.wait_for_stop, args=(stop,))
            waiter.start()
            time.sleep(0.5)
            self.assertTrue(waiter.is_alive())
            stop.write_text("", encoding="utf-8")
            waiter.join(timeout=3)
            self.assertFalse(waiter.is_alive())
            self.assertFalse(stop.exists(), "the stop file is consumed")

    def test_custom_milestone_file_is_polled(self):
        with tempfile.TemporaryDirectory() as root:
            milestone = Path(root) / "MILESTONE"
            manager = snapshot.SnapshotManager(Path(root) / "episode", Path(root), writer=None, enabled=False, milestone_file=milestone)
            names = []
            manager.notify_milestone = lambda name, t_ms: names.append(name)
            poller = threading.Thread(target=manager._milestone_file_poll_loop, daemon=True)
            manager._stop = False
            poller.start()
            milestone.write_text("连杆完成", encoding="utf-8")
            for _ in range(20):
                if names:
                    break
                time.sleep(0.2)
            manager._stop = True
            self.assertEqual(names, ["连杆完成"])
            self.assertFalse(milestone.exists())

    @unittest.skipUnless(snapshot.check_git_available(), "git not installed")
    def test_ignore_patterns_can_stay_out_of_the_watched_directory(self):
        with tempfile.TemporaryDirectory() as root:
            watch = Path(root) / "task"
            watch.mkdir()
            manager = snapshot.SnapshotManager(Path(root) / "episode", watch, writer=None, watch_gitignore=False)
            self.assertTrue(manager.init_repo(), manager.disabled_reason)
            self.assertEqual(list(watch.iterdir()), [], "nothing is written into the watched directory")
            (watch / "~$part.SLDPRT").write_text("lock", encoding="utf-8")
            (watch / "part.SLDPRT").write_text("model", encoding="utf-8")
            r = snapshot.run_git(manager.git_dir, manager.watch_dir, ["status", "--porcelain", "--untracked-files=all"])
            self.assertEqual(r.stdout.split(), ["??", "part.SLDPRT"])

    @unittest.skipUnless(snapshot.check_git_available(), "git not installed")
    def test_ignore_patterns_default_to_the_watched_gitignore(self):
        with tempfile.TemporaryDirectory() as root:
            manager = snapshot.SnapshotManager(Path(root) / "episode", Path(root) / "task", writer=None)
            self.assertTrue(manager.init_repo(), manager.disabled_reason)
            self.assertIn("~$*", (Path(root) / "task" / ".gitignore").read_text(encoding="utf-8").splitlines())


if __name__ == "__main__":
    unittest.main()
