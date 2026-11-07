"""Exercise approved-batch adaptation with an entirely fake hhbot package."""

from __future__ import annotations

import copy
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import backend_bridge


class FakeConfig:
    def __init__(self, raw, path):
        self.raw = raw
        self.path = path


class BackendBridgeTests(unittest.TestCase):
    def setUp(self):
        self.rows = {
            1: {"id": 1, "title": "First", "score": 100, "status": "new"},
            2: {"id": 2, "title": "Unapproved high rank", "score": 120, "status": "new"},
            3: {"id": 3, "title": "Selected lower rank", "score": 90, "status": "checked"},
        }
        rows = self.rows

        class FakeStore:
            def __init__(self, *args, **kwargs):
                pass

            def vacancy(self, vacancy_id):
                return copy.deepcopy(rows.get(vacancy_id))

        self.config = FakeConfig({
            "notifications": {"enabled": True},
            "apply": {"save_screenshots": True, "cover_letter": {"enabled": True, "text": "Default"}},
        }, Path("/fixture/config.yaml"))
        self.cli = types.ModuleType("hhbot.cli")
        self.cli.Store = FakeStore
        self.cli.apply_to_vacancy = mock.Mock(return_value="applied")
        self.original_apply = self.cli.apply_to_vacancy
        self.cli.setup_logging = mock.Mock(return_value=object())
        self.cli.cmd_login = mock.Mock(return_value=0)
        self.cli.cmd_search = mock.Mock(return_value=0)
        self.cli.cmd_apply = mock.Mock(return_value=0)
        self.cli._pause_for_captcha = mock.Mock()
        package = types.ModuleType("hhbot")
        package.__path__ = []
        package.cli = self.cli
        state = types.ModuleType("hhbot.state")
        state.Store = FakeStore
        package.state = state
        modules = mock.patch.dict(sys.modules, {"hhbot": package, "hhbot.cli": self.cli, "hhbot.state": state})
        modules.start()
        self.addCleanup(modules.stop)
        loader = mock.patch.object(backend_bridge, "load_config", return_value=(Path("/fixture"), self.config, self.config.path))
        loader.start()
        self.addCleanup(loader.stop)
        self.plan = {
            "action": "apply", "limit": 2, "min_score": 70,
            "vacancies": [self.rows[3], self.rows[1]],
            "letters": [
                {"vacancy_id": 3, "letter": "Письмо для выбранной третьей\nВиктор"},
                {"vacancy_id": 1, "letter": "Письмо для первой"},
            ],
        }

    def test_store_exposes_only_approved_ids_in_approved_order(self):
        backend_bridge.execute(self.plan)
        selected = self.cli.Store("not-a-real-database")
        full = selected.candidates(70, limit=50, statuses=("new", "checked"))
        self.assertEqual([v["id"] for v in full], [3, 1])
        short = selected.candidates(70, limit=1, statuses=("new", "checked"))
        self.assertEqual([v["id"] for v in short], [3])
        self.assertNotIn(2, [v["id"] for v in full])

    def test_missing_changed_or_below_threshold_selection_stops_batch(self):
        backend_bridge.execute(self.plan)
        selected = self.cli.Store()
        for change in ({"status": "applied"}, {"score": 69}, None):
            with self.subTest(change=change):
                self.rows[3] = ({"id": 3, "score": 90, "status": "checked", **change}
                                if change is not None else None)
                with self.assertRaises(ValueError):
                    selected.candidates(70, limit=50, statuses=("new", "checked"))
        self.original_apply.assert_not_called()

    def test_each_selected_vacancy_gets_its_exact_planned_letter(self):
        backend_bridge.execute(self.plan)
        original_letter = copy.deepcopy(self.config.raw["apply"]["cover_letter"])
        page = object()
        for vacancy_id, expected in ((1, "Письмо для первой"), (3, "Письмо для выбранной третьей\nВиктор")):
            self.cli.apply_to_vacancy(page, self.rows[vacancy_id], self.config, dry_run=False)
            args, kwargs = self.original_apply.call_args
            selected_config = args[2]
            self.assertEqual(selected_config.raw["apply"]["cover_letter"], {"enabled": True, "text": expected})
            self.assertIsNot(selected_config, self.config)
            self.assertEqual(kwargs, {"dry_run": False})
        self.assertEqual(self.config.raw["apply"]["cover_letter"], original_letter)

    def test_unapproved_vacancy_never_reaches_original_apply(self):
        backend_bridge.execute(self.plan)
        with self.assertRaises(KeyError):
            self.cli.apply_to_vacancy(object(), self.rows[2], self.config)
        self.original_apply.assert_not_called()

    def test_check_batch_keeps_live_false_and_disables_unapproved_side_effects(self):
        self.plan["action"] = "check"
        backend_bridge.execute(self.plan)
        args, config, _ = self.cli.cmd_apply.call_args.args
        self.assertIs(args.live, False)
        self.assertEqual(args.limit, 2)
        self.assertEqual(args.top, 2)
        self.assertEqual(config.raw["notifications"], {"enabled": False})
        self.assertIs(config.raw["apply"]["save_screenshots"], False)
        self.assertIs(config.raw["apply"]["skip_questions"], True)
        with self.assertRaisesRegex(RuntimeError, "Капча"):
            self.cli._pause_for_captcha(object())


if __name__ == "__main__":
    unittest.main()
