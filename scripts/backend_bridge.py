#!/usr/bin/env python3
"""Fixed operations against a trusted cv_bot workspace; no browser imports on reads."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys


def source_hash(root):
    digest = hashlib.sha256()
    for path in sorted((root / "hhbot").glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def load_config(request):
    root = Path(request["workspace"]).resolve()
    sys.path.insert(0, str(root))
    try:
        from hhbot.config import Config
    except ModuleNotFoundError as exc:
        raise ValueError("Не установлены зависимости cv_bot. Используйте его .venv и requirements.txt.") from exc
    config_path = Path(request.get("config_path") or root / "config.yaml").resolve()
    return root, Config.load(str(config_path)), config_path


def inspect(request):
    root, cfg, config_path = load_config(request)
    # Do not use Config.data_dir/profile_dir: those properties create directories.
    data = root / cfg.get("paths", "data_dir", default="data")
    return {"workspace": str(root), "config_path": str(config_path), "python": sys.executable,
            "db_path": str((data / cfg.get("paths", "db_file", default="hh_bot.sqlite3")).resolve()),
            "profile_dir": str((root / cfg.get("hh", "profile_dir", default=".chrome-profile")).resolve()),
            "debug_dir": str((root / cfg.get("paths", "debug_dir", default="data/debug")).resolve()),
            "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "source_sha256": source_hash(root), "queries": cfg.queries,
            "min_score": cfg.min_score, "min_salary": cfg.min_salary,
            "max_per_day": int(cfg.get("apply", "max_per_day", default=25)),
            "max_per_run": int(cfg.get("apply", "max_per_run", default=10)),
            "cover_letter": cfg.cover_letter, "base_url": cfg.base_url}


def score(request):
    _, cfg, _ = load_config(request)
    from hhbot.score import score_vacancy
    value = score_vacancy(request["vacancy"], cfg)
    return {"score": value.value, "matched": value.matched, "veto": value.veto,
            "passes_filters": value.passed, "meets_min_score": value.passed and value.value >= cfg.min_score}


def execute(plan):
    """Called exclusively by run_action.py after its approval flags and validation."""
    _, cfg, _ = load_config(plan)
    from hhbot import cli
    from hhbot.state import Store
    cfg.raw["notifications"] = {"enabled": False}
    cfg.raw.setdefault("apply", {})["save_screenshots"] = False
    cfg.raw["apply"]["skip_questions"] = True

    if plan["action"] in ("apply", "check"):
        approved_ids = [v["id"] for v in plan["vacancies"]]
        planned_letters = {v["vacancy_id"]: v["letter"] for v in plan["letters"]}
        original_apply = cli.apply_to_vacancy

        def apply_with_planned_letter(page, vacancy, config, **kwargs):
            raw = copy.deepcopy(config.raw)
            letter = planned_letters[vacancy["id"]]
            raw.setdefault("apply", {}).setdefault("cover_letter", {}).update(enabled=bool(letter), text=letter)
            selected_config = type(config)(raw, config.path)
            return original_apply(page, vacancy, selected_config, **kwargs)

        cli.apply_to_vacancy = apply_with_planned_letter

        class SelectedStore(Store):
            def candidates(self, min_score, limit=50, statuses=("new",)):
                # The legacy loop can process limit+1 candidates. Expose exactly the approved list.
                rows = []
                for vid in approved_ids:
                    row = self.vacancy(vid)
                    if row is None or row["status"] not in statuses or row["score"] < min_score:
                        raise ValueError("Статус выбранной вакансии изменился. Остановка без расширения очереди.")
                    rows.append(row)
                return rows[:min(limit, len(approved_ids))]

        cli.Store = SelectedStore

        def stop_on_captcha(page):
            # Do not resume a partially completed live batch after a captcha.
            raise RuntimeError("Капча. Запуск остановлен; перед продолжением проверьте отправленные отклики.")

        cli._pause_for_captcha = stop_on_captcha
    log = cli.setup_logging(cfg)
    args = argparse.Namespace(top=plan["limit"], limit=plan["limit"], min_score=plan["min_score"],
                              live=plan["action"] == "apply", yes=True)
    command = {"login": cli.cmd_login, "search": cli.cmd_search,
               "check": cli.cmd_apply, "apply": cli.cmd_apply}[plan["action"]]
    return int(command(args, cfg, log) or 0)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--execute-plan":
        # Guard against casually invoking the internal bridge directly.
        if os.environ.get("VB_CV_BOT_APPROVED_RUN") != "1":
            raise SystemExit("Используйте run_action.py с флагами разрешённого запуска.")
        plan = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        raise SystemExit(execute(plan))
    try:
        request = json.load(sys.stdin)
        operation = request["operation"]
        result = {"inspect": inspect, "score": score}[operation](request)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
