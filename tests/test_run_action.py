"""Runner authorization and plan lifecycle; every subprocess is mocked."""

from __future__ import annotations

import copy
import datetime as dt
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.cvbot import CVBot, CVError, atomic_json
from scripts.run_action import run


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cvbot-runner-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.bot = CVBot(self.root / "plugin-home")
        self.plan_id = "1" * 32
        self.plan = {
            "plan_id": self.plan_id,
            "action": "search",
            "status": "prepared",
            "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat(),
            "workspace": str(self.workspace),
            "config_path": str(self.workspace / "config.yaml"),
            "python": sys.executable,
            "db_path": str(self.workspace / "never-create.sqlite3"),
            "config_sha256": "a" * 64,
            "source_sha256": "b" * 64,
            "vacancies": [],
            "letters": [],
            "browser_started": False,
        }
        self.plan_path = self.bot.home / "plans" / f"{self.plan_id}.json"
        self._save()
        inspect = mock.patch.object(self.bot, "_inspect", return_value=copy.deepcopy(self.plan))
        inspect.start()
        self.addCleanup(inspect.stop)
        process = mock.patch("scripts.run_action.subprocess.run")
        self.process = process.start()
        self.addCleanup(process.stop)
        self.process.return_value = subprocess.CompletedProcess(args=["fake-backend"], returncode=0)

    def _save(self):
        atomic_json(self.plan_path, self.plan)

    def test_missing_browser_authorization_never_starts_subprocess(self):
        with self.assertRaises(CVError):
            run(self.bot, self.plan_id)
        self.process.assert_not_called()
        self.assertEqual(self.bot.get_run(self.plan_id)["status"], "prepared")

    def test_apply_without_send_authorization_never_starts_subprocess(self):
        self.plan["action"] = "apply"
        self._save()
        with self.assertRaises(CVError):
            run(self.bot, self.plan_id, allow_browser=True)
        self.process.assert_not_called()
        self.assertEqual(self.bot.get_run(self.plan_id)["status"], "prepared")

    def test_non_tty_login_is_refused_before_subprocess(self):
        self.plan["action"] = "login"
        self._save()
        with mock.patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(CVError):
                run(self.bot, self.plan_id, allow_browser=True)
        self.process.assert_not_called()
        self.assertEqual(self.bot.get_run(self.plan_id)["status"], "prepared")

    def test_used_plan_cannot_start_subprocess(self):
        self.plan["status"] = "completed"
        self._save()
        with self.assertRaises(CVError):
            run(self.bot, self.plan_id, allow_browser=True)
        self.process.assert_not_called()

    def test_success_saves_running_before_subprocess_and_completed_after(self):
        observed = []

        def fake_backend(*args, **kwargs):
            observed.append(self.bot.get_run(self.plan_id))
            return subprocess.CompletedProcess(args=args[0], returncode=0)

        self.process.side_effect = fake_backend
        self.assertEqual(run(self.bot, self.plan_id, allow_browser=True), 0)
        self.process.assert_called_once()
        self.assertEqual(observed[0]["status"], "running")
        self.assertTrue(observed[0]["browser_start_attempted"])
        self.assertIsNone(observed[0]["browser_started"])
        completed = self.bot.get_run(self.plan_id)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["exit_code"], 0)
        self.assertTrue(completed["browser_start_attempted"])
        self.assertIsNone(completed["browser_started"])
        self.assertIn("finished_at", completed)
        self.assertFalse((self.workspace / "never-create.sqlite3").exists())
        with self.assertRaises(CVError):
            run(self.bot, self.plan_id, allow_browser=True)
        self.process.assert_called_once()

    def test_nonzero_backend_return_marks_plan_failed(self):
        self.process.return_value = subprocess.CompletedProcess(args=["fake-backend"], returncode=7)
        self.assertEqual(run(self.bot, self.plan_id, allow_browser=True), 7)
        failed = self.bot.get_run(self.plan_id)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["exit_code"], 7)
        self.assertTrue(failed["browser_start_attempted"])
        self.assertIn("finished_at", failed)

    def test_backend_exception_marks_plan_failed_without_retry(self):
        self.process.side_effect = OSError("fixture backend is unavailable")
        with self.assertRaises(OSError):
            run(self.bot, self.plan_id, allow_browser=True)
        failed = self.bot.get_run(self.plan_id)
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["browser_start_attempted"])
        self.assertIn("finished_at", failed)
        self.process.assert_called_once()


if __name__ == "__main__":
    unittest.main()
