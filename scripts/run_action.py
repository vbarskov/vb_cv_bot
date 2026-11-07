#!/usr/bin/env python3
"""Execute one prepared browser plan, only after the user authorized its effects."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.cvbot import CVBot, CVError, ROOT, atomic_json, fingerprint


def run(bot, plan_id, allow_browser=False, allow_send=False):
    plan = bot.get_run(plan_id)
    if not allow_browser:
        raise CVError("Нужно явное разрешение пользователя на этот запуск браузера (--allow-browser).")
    if plan["action"] == "apply" and not allow_send:
        raise CVError("Нужно разрешение на отправку выбранных откликов (--allow-send).")
    if plan["action"] == "login" and not sys.stdin.isatty():
        raise CVError("Для login нужен интерактивный терминал (exec_command tty:true).")
    lock_dir = bot.home / "locks"
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = lock_dir / (fingerprint(plan["workspace"]) + ".lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CVError("Другой запуск этого проекта уже выполняется.") from None
        # Re-read after obtaining the lock: a parallel process may have consumed this plan.
        plan = bot.get_run(plan_id)
        bot.validate_plan(plan)
        plan.update(status="running", started_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                    browser_started=None, browser_start_attempted=True)
        plan_path = bot.home / "plans" / (plan_id + ".json")
        atomic_json(plan_path, plan)
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "VB_CV_BOT_APPROVED_RUN": "1"}
        try:
            result = subprocess.run([plan["python"], "-B", str(ROOT / "scripts/backend_bridge.py"),
                                     "--execute-plan", str(plan_path)], cwd=plan["workspace"], env=env,
                                    check=False)
            plan.update(status="completed" if result.returncode == 0 else "failed", exit_code=result.returncode)
            return result.returncode
        except KeyboardInterrupt:
            plan.update(status="interrupted", outcome="Проверьте историю откликов перед новым запуском.")
            raise
        except BaseException:
            plan.update(status="failed", outcome="Запуск не завершён. Проверьте историю откликов.")
            raise
        finally:
            plan["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            atomic_json(plan_path, plan)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan_id")
    parser.add_argument("--allow-browser", action="store_true")
    parser.add_argument("--allow-send", action="store_true")
    args = parser.parse_args()
    try:
        return run(CVBot(), args.plan_id, args.allow_browser, args.allow_send)
    except (CVError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
