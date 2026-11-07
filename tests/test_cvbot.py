"""Contract tests using a disposable copy of the legacy SQLite schema.

The source bot, its browser, and its real configuration are never imported.
"""

from __future__ import annotations

import copy
from contextlib import closing
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.cvbot import CVBot


# Kept independent of hhbot.state.Store: importing Store would hide accidental
# schema creation/migration in plugin read operations.
LEGACY_SCHEMA = """
CREATE TABLE vacancies (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, company TEXT, url TEXT,
    salary_text TEXT, address TEXT, remote INTEGER DEFAULT 0, experience TEXT,
    score INTEGER DEFAULT 0, matched TEXT, query TEXT, mode TEXT,
    found_at TEXT NOT NULL, last_seen_at TEXT,
    status TEXT NOT NULL DEFAULT 'new', note TEXT
);
CREATE INDEX idx_vacancies_status ON vacancies(status, score);
CREATE INDEX idx_vacancies_score ON vacancies(score DESC);
CREATE TABLE applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, vacancy_id INTEGER NOT NULL,
    status TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL
);
CREATE INDEX idx_applications_created ON applications(created_at);
CREATE INDEX idx_applications_vacancy ON applications(vacancy_id);
"""


class CVBotTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cvbot-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "source workspace"
        self.workspace.mkdir()
        (self.workspace / "hhbot").mkdir()
        for name in ("config", "score", "state", "cli"):
            (self.workspace / "hhbot" / f"{name}.py").write_text(
                "# fixture only; must never be executed\n", encoding="utf-8"
            )
        self.config = self.workspace / "config.yaml"
        self.config.write_text("# isolated fixture; never loaded\n", encoding="utf-8")
        self.db = self.workspace / "data" / "state.sqlite3"
        self.db.parent.mkdir()
        self.home = self.root / "plugin-home"
        self.snapshot = {
            "workspace": str(self.workspace),
            "config_path": str(self.config),
            "python": sys.executable,
            "db_path": str(self.db),
            "profile_dir": str(self.workspace / "browser-profile"),
            "debug_dir": str(self.workspace / "debug"),
            "config_sha256": "a" * 64,
            "source_sha256": "b" * 64,
            "queries": ["Product Engineer"],
            "min_score": 70,
            "min_salary": 150000,
            "max_per_day": 5,
            "max_per_run": 2,
            "cover_letter": "Здравствуйте, {company}! Интересует {title}: {url}",
            "base_url": "https://hh.ru",
        }
        self.bot = CVBot(self.home)
        self.inspect = mock.patch.object(
            self.bot, "_inspect", side_effect=lambda *args, **kwargs: copy.deepcopy(self.snapshot)
        ).start()
        self.addCleanup(mock.patch.stopall)
        with mock.patch.object(self.bot, "_bridge", return_value=copy.deepcopy(self.snapshot)):
            self.bot.connect_workspace(str(self.workspace), str(self.config))
        self._create_database()
        self.process_guard = mock.patch(
            "subprocess.run", side_effect=AssertionError("test must never execute source or browser")
        ).start()

    def _create_database(self):
        now = dt.datetime.now().isoformat(timespec="seconds")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript(LEGACY_SCHEMA)
            for vacancy_id, score, status in (
                (1, 100, "new"),
                (2, 95, "new"),
                (3, 90, "checked"),
                (4, 60, "new"),
                (5, 99, "skipped"),
                (6, 110, "applied"),
            ):
                conn.execute(
                    """INSERT INTO vacancies
                       (id,title,company,url,score,found_at,last_seen_at,status,note)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (vacancy_id, f"Engineer {vacancy_id}", "Example", f"https://hh.ru/vacancy/{vacancy_id}",
                     score, now, now, status, ""),
                )
            conn.execute(
                "INSERT INTO applications(vacancy_id,status,note,created_at) VALUES(?,?,?,?)",
                (6, "applied", "existing", now),
            )

    def _rows(self, query, params=()):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            return conn.execute(query, params).fetchall()

    def _database_digest(self):
        return hashlib.sha256(self.db.read_bytes()).hexdigest()

    def test_connection_is_persisted_only_under_plugin_home(self):
        connection = json.loads((self.home / "connection.json").read_text(encoding="utf-8"))
        self.assertEqual(connection["workspace"], str(self.workspace))
        self.assertEqual(connection["config_path"], str(self.config))
        self.assertFalse((self.workspace / "connection.json").exists())

    def test_read_operations_leave_legacy_database_and_source_untouched(self):
        before = self._database_digest()
        before_files = sorted(str(p.relative_to(self.workspace)) for p in self.workspace.rglob("*"))
        with mock.patch("subprocess.run", side_effect=AssertionError("must not execute source")):
            self.assertIsInstance(self.bot.status(), dict)
            rows = self.bot.list_vacancies(statuses=["new"], min_score=70, limit=1)["vacancies"]
            vacancy = self.bot.get_vacancy(1)
        self.assertEqual([row["id"] for row in rows], [1])
        self.assertEqual(vacancy["title"], "Engineer 1")
        self.assertEqual(before, self._database_digest())
        self.assertEqual(before_files, sorted(str(p.relative_to(self.workspace)) for p in self.workspace.rglob("*")))

    def test_missing_database_is_never_created_by_reads(self):
        self.db.unlink()
        for operation in (
            self.bot.status,
            lambda: self.bot.list_vacancies(),
            lambda: self.bot.get_vacancy(1),
        ):
            try:
                operation()
            except (ValueError, RuntimeError, FileNotFoundError):
                # An unavailable-data response or a domain error are both valid.
                pass
            self.assertFalse(self.db.exists())
        self.assertFalse((self.workspace / "browser-profile").exists())

    def test_filters_use_status_and_score_together(self):
        rows = self.bot.list_vacancies(statuses=["new", "checked"], min_score=90, limit=20)["vacancies"]
        self.assertEqual([row["id"] for row in rows], [1, 2, 3])

    def test_sql_like_status_is_rejected_without_mutating_database(self):
        before = self._database_digest()
        with self.assertRaises((ValueError, RuntimeError)):
            self.bot.list_vacancies(statuses=["new') OR 1=1 --"])
        with self.assertRaises((ValueError, RuntimeError)):
            self.bot.mark_vacancy(1, "applied'; DROP TABLE vacancies; --")
        self.assertEqual(before, self._database_digest())

    def test_unknown_vacancy_cannot_create_application(self):
        with self.assertRaises((ValueError, RuntimeError)):
            self.bot.mark_vacancy(999, "applied")
        self.assertEqual(self._rows("SELECT vacancy_id FROM applications"), [(6,)])

    def test_manual_applied_is_atomic_and_idempotent(self):
        self.bot.mark_vacancy(1, "applied")
        self.bot.mark_vacancy(1, "applied")
        self.assertEqual(self._rows("SELECT status FROM vacancies WHERE id=1"), [("applied",)])
        self.assertEqual(
            self._rows("SELECT status,note FROM applications WHERE vacancy_id=1"),
            [("applied", "manual")],
        )

    def test_application_log_failure_rolls_back_vacancy_update(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("""CREATE TRIGGER deny_application BEFORE INSERT ON applications
                            BEGIN SELECT RAISE(ABORT, 'fixture write rejected'); END""")
        with self.assertRaises((sqlite3.DatabaseError, ValueError, RuntimeError)):
            self.bot.mark_vacancy(1, "applied")
        self.assertEqual(self._rows("SELECT status FROM vacancies WHERE id=1"), [("new",)])
        self.assertEqual(self._rows("SELECT vacancy_id FROM applications"), [(6,)])

    def test_applied_cannot_return_to_candidate_status(self):
        for status in ("new", "skipped"):
            with self.subTest(status=status):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.bot.mark_vacancy(6, status)
        self.assertEqual(self._rows("SELECT status FROM vacancies WHERE id=6"), [("applied",)])

    def test_letter_draft_uses_vacancy_values_without_database_writes(self):
        before = self._database_digest()
        draft = self.bot.draft_letter(1, "{title} | {company} | {url}")
        self.assertEqual(draft["letter"], "Engineer 1 | Example | https://hh.ru/vacancy/1")
        self.assertIs(draft["sent"], False)
        self.assertEqual(self.bot.draft_letter(1, "")["letter"], "")
        self.assertEqual(before, self._database_digest())

    def test_check_and_apply_plans_select_only_eligible_rows_and_enforce_run_cap(self):
        before = self._database_digest()
        for action in ("check", "apply"):
            with self.subTest(action=action):
                plan = self.bot.prepare_run(action, limit=4, min_score=70)
                self.assertRegex(plan["plan_id"], r"^[0-9a-f]{32}$")
                self.assertEqual(plan["action"], action)
                self.assertEqual(len(plan["vacancies"]), 2)
                self.assertTrue({row["id"] for row in plan["vacancies"]}.issubset({1, 2, 3}))
                self.assertEqual(self.bot.get_run(plan["plan_id"])["plan_id"], plan["plan_id"])
                self.assertTrue((self.home / "plans" / f"{plan['plan_id']}.json").is_file())
        self.assertEqual(before, self._database_digest())

    def test_plan_size_respects_remaining_daily_quota(self):
        self.snapshot["max_per_day"] = 2  # One existing application today.
        plan = self.bot.prepare_run("apply", limit=2, min_score=70)
        self.assertEqual(len(plan["vacancies"]), 1)

    def test_individual_letters_are_preserved_exactly_in_selected_order(self):
        before = self._database_digest()
        letters = [
            {"vacancy_id": 2, "letter": "Текст для второй\n\nС уважением, Виктор\n"},
            {"vacancy_id": 1, "letter": "  Точный текст для первой.  "},
        ]
        plan = self.bot.prepare_run("apply", limit=2, letters=letters)
        self.assertEqual(plan["letters"], [letters[1], letters[0]])
        persisted = self.bot.get_run(plan["plan_id"])
        self.assertEqual(persisted["letters"], plan["letters"])
        self.assertEqual(before, self._database_digest())

    def test_missing_mismatched_and_duplicate_letters_are_rejected(self):
        for letters in (
            [],
            [{"vacancy_id": 1, "letter": "Only one"}],
            [{"vacancy_id": 1, "letter": "First"}, {"vacancy_id": 3, "letter": "Wrong selection"}],
            [{"vacancy_id": 1, "letter": "First"}, {"vacancy_id": 1, "letter": "Duplicate"}],
        ):
            with self.subTest(letters=letters):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.bot.prepare_run("apply", limit=2, letters=letters)
        self.assertFalse((self.home / "plans").exists())

    def test_individual_letters_reject_placeholders_and_non_apply_action(self):
        for action, letters in (
            ("apply", [{"vacancy_id": 1, "letter": "Hello {company}"}]),
            ("check", [{"vacancy_id": 1, "letter": "Already finished"}]),
            ("search", []),
        ):
            with self.subTest(action=action):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.bot.prepare_run(action, limit=1, letters=letters)

    def test_finished_individual_letter_can_replace_incomplete_default_template(self):
        self.snapshot["cover_letter"] = "Hello {unresolved}"
        letters = [{"vacancy_id": 1, "letter": "Полностью готовый текст"}]
        plan = self.bot.prepare_run("apply", limit=1, letters=letters)
        self.assertEqual(plan["letters"], letters)

    def test_explicit_selection_can_choose_lower_ranked_candidate(self):
        letters = [{"vacancy_id": 3, "letter": "Специально для Engineer 3"}]
        plan = self.bot.prepare_run("apply", limit=1, vacancy_ids=[3], letters=letters)
        self.assertEqual([v["id"] for v in plan["vacancies"]], [3])
        self.assertEqual(plan["letters"], letters)

    def test_explicit_selection_preserves_user_order(self):
        plan = self.bot.prepare_run("check", limit=2, vacancy_ids=[3, 1])
        self.assertEqual([v["id"] for v in plan["vacancies"]], [3, 1])

    def test_explicit_selection_rejects_invalid_or_ineligible_candidates(self):
        for ids in ([], [1, 1], [999], [4], [5], [6]):
            with self.subTest(vacancy_ids=ids):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.bot.prepare_run("apply", limit=2, vacancy_ids=ids)
        self.assertFalse((self.home / "plans").exists())

    def test_explicit_selection_cannot_exceed_requested_limit_or_run_cap(self):
        for ids, limit in (([1, 2], 1), ([1, 2, 3], 3)):
            with self.subTest(vacancy_ids=ids, limit=limit):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.bot.prepare_run("apply", limit=limit, vacancy_ids=ids)

    def test_explicit_selection_cannot_exceed_remaining_daily_quota(self):
        self.snapshot["max_per_day"] = 2
        with self.assertRaises((ValueError, RuntimeError)):
            self.bot.prepare_run("apply", limit=2, vacancy_ids=[1, 2])

    def test_exhausted_daily_quota_creates_no_plan(self):
        self.snapshot["max_per_day"] = 1
        with self.assertRaises((ValueError, RuntimeError)):
            self.bot.prepare_run("apply", limit=2)
        self.assertFalse((self.home / "plans").exists())

    def test_candidate_change_invalidates_prepared_plan(self):
        plan = self.bot.prepare_run("apply", limit=1)
        selected = plan["vacancies"][0]["id"]
        self.bot.mark_vacancy(selected, "skipped")
        with self.assertRaises((ValueError, RuntimeError)):
            self.bot.validate_plan(plan)

    def test_expired_or_used_plan_cannot_be_validated(self):
        plan = self.bot.prepare_run("apply", limit=1)
        plan["expires_at"] = "2000-01-01T00:00:00+00:00"
        with self.assertRaises((ValueError, RuntimeError)):
            self.bot.validate_plan(plan)
        plan = self.bot.prepare_run("apply", limit=1)
        plan["status"] = "completed"
        with self.assertRaises((ValueError, RuntimeError)):
            self.bot.validate_plan(plan)

    def test_plan_fingerprint_rejects_config_and_source_drift(self):
        for key in ("config_sha256", "source_sha256"):
            with self.subTest(key=key):
                original = self.snapshot[key]
                plan = self.bot.prepare_run("apply", limit=1)
                self.snapshot[key] = "c" * 64
                with self.assertRaises((ValueError, RuntimeError)):
                    self.bot.validate_plan(plan)
                self.snapshot[key] = original

    def test_invalid_limits_actions_and_plan_paths_are_rejected(self):
        for limit in (0, -1, 1001):
            with self.subTest(limit=limit):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.bot.prepare_run("apply", limit=limit)
        with self.assertRaises((ValueError, RuntimeError)):
            self.bot.prepare_run("shell", limit=1)
        for plan_id in ("../connection", "a" * 31, "g" * 32, "/etc/passwd"):
            with self.subTest(plan_id=plan_id):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.bot.get_run(plan_id)


if __name__ == "__main__":
    unittest.main()
