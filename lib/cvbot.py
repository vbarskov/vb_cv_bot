"""Local queue tools. Browser execution lives in the separately approved runner."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parent.parent
STATUSES = frozenset(("new", "filtered", "checked", "applied", "already_applied",
                      "manual_required", "closed", "failed", "presented", "skipped"))
ELIGIBLE = ("new", "checked")
TERMINAL = ("applied", "already_applied")


class CVError(ValueError):
    """A user-facing configuration or workflow error."""


def bounded_int(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise CVError(f"{name}: требуется целое число от {low} до {high}")
    return value


def atomic_json(path: Path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def render_letter(template, vacancy):
    """Pure rendering of the original bot's title/company/url placeholders."""
    title = str(vacancy.get("title") or "").strip()
    if len(title) > 60:
        title = title[:57].rstrip(" •—-,/") + "…"
    values = {"title": title, "company": str(vacancy.get("company") or "").strip(),
              "url": str(vacancy.get("url") or "").strip()}
    result = re.sub(r"\{(\w+)\}", lambda m: values.get(m[1].lower(), m[0]), str(template or ""))
    result = re.sub(r"«\s*»|\(\s*\)", "", result)
    result = re.sub(r"[ \t]{2,}", " ", result)
    return re.sub(r"\s+([.,;:!?])", r"\1", result).strip()


class CVBot:
    def __init__(self, home=None):
        self.home = Path(home or os.environ.get("VB_CV_BOT_HOME") or
                         Path.home() / ".local/share/vb-cv-bot").expanduser().resolve()

    def _connection(self):
        path = self.home / "connection.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        workspace = os.environ.get("VB_CV_BOT_WORKSPACE")
        if workspace:
            return {"workspace": str(Path(workspace).expanduser().resolve()), "config_path": None}
        raise CVError("Сначала подключите cv_bot через cv_connect_workspace(workspace).")

    @staticmethod
    def _workspace(workspace, config_path=None):
        root = Path(workspace).expanduser().resolve()
        for name in ("config", "score", "state", "cli"):
            if not (root / "hhbot" / (name + ".py")).is_file():
                raise CVError(f"Не найден hhbot/{name}.py в выбранном проекте")
        config = Path(config_path).expanduser() if config_path else root / "config.yaml"
        if not config.is_absolute():
            config = root / config
        config = config.resolve()
        if not config.is_file():
            raise CVError(f"Не найден конфиг: {config}")
        return {"workspace": str(root), "config_path": str(config)}

    @staticmethod
    def _python(workspace):
        for name in ("python3", "python"):
            path = Path(workspace) / ".venv/bin" / name
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        return sys.executable

    def _bridge(self, operation, payload, connection=None):
        connection = connection or self._connection()
        workspace = connection["workspace"]
        request = {**connection, "operation": operation, **payload}
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run(
            [self._python(workspace), "-B", str(ROOT / "scripts/backend_bridge.py")],
            input=json.dumps(request), capture_output=True, text=True, cwd=workspace,
            timeout=20, env=env, check=False,
        )
        try:
            data = json.loads(result.stdout)
        except ValueError:
            raise CVError("Не удалось прочитать конфиг. Проверьте Python и зависимости cv_bot.") from None
        if result.returncode or "error" in data:
            raise CVError(data.get("error", "Ошибка backend cv_bot"))
        return data

    def _inspect(self):
        return self._bridge("inspect", {})

    def connect_workspace(self, workspace, config_path=None):
        connection = self._workspace(workspace, config_path)
        info = self._bridge("inspect", {}, connection)
        atomic_json(self.home / "connection.json", connection)
        return {"connected": True, "workspace": info["workspace"],
                "db_path": info["db_path"], "database_exists": Path(info["db_path"]).is_file(),
                "browser_started": False}

    @contextmanager
    def _db(self, info, writable=False):
        path = Path(info["db_path"]).expanduser().resolve()
        if not path.is_file():
            raise CVError("База вакансий ещё не существует. Подготовьте поиск через cv_prepare_run(action='search').")
        mode = "rw" if writable else "ro"
        conn = sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True, timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            if not writable:
                conn.execute("PRAGMA query_only = ON")
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _stats(conn):
        counts = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM vacancies GROUP BY status")}
        today = dt.date.today().isoformat()
        applied_today = conn.execute(
            "SELECT COUNT(*) FROM applications WHERE DATE(created_at)=? AND status IN ('applied','already_applied')",
            (today,),
        ).fetchone()[0]
        return {"by_status": counts, "total_vacancies": sum(counts.values()), "applied_today": applied_today}

    def status(self):
        try:
            self._connection()
        except CVError:
            return {"connected": False, "browser_started": False}
        info = self._inspect()
        result = {k: info[k] for k in ("workspace", "config_path", "db_path", "queries", "min_score",
                                        "min_salary", "max_per_day", "max_per_run")}
        result.update(connected=True, database_exists=Path(info["db_path"]).is_file(), browser_started=False)
        if result["database_exists"]:
            with self._db(info) as conn:
                result.update(self._stats(conn))
                result["recent_applications"] = [dict(r) for r in conn.execute(
                    "SELECT a.vacancy_id,a.created_at,a.status,a.note,v.title,v.company,v.url "
                    "FROM applications a LEFT JOIN vacancies v ON a.vacancy_id=v.id ORDER BY a.id DESC LIMIT 15"
                )]
        else:
            result.update(by_status={}, total_vacancies=0, applied_today=0, recent_applications=[])
        return result

    def list_vacancies(self, statuses=None, min_score=None, limit=20):
        bounded_int(limit, "limit", 1, 100)
        if statuses is None:
            statuses = list(ELIGIBLE)
        if not isinstance(statuses, list) or not statuses or any(s not in STATUSES for s in statuses):
            raise CVError("statuses: требуется непустой список известных статусов вакансий")
        info = self._inspect()
        minimum = info["min_score"] if min_score is None else bounded_int(min_score, "min_score", 0, 200)
        with self._db(info) as conn:
            rows = conn.execute(
                "SELECT * FROM vacancies WHERE status IN (%s) AND score>=? ORDER BY score DESC,id DESC LIMIT ?"
                % ",".join("?" * len(statuses)), (*statuses, minimum, limit),
            )
            return {"vacancies": [dict(row) for row in rows], "min_score": minimum,
                    "statuses": statuses, "limit": limit}

    @staticmethod
    def _vacancy(conn, vacancy_id):
        bounded_int(vacancy_id, "vacancy_id", 1, 2**63 - 1)
        row = conn.execute("SELECT * FROM vacancies WHERE id=?", (vacancy_id,)).fetchone()
        if row is None:
            raise CVError(f"Вакансия {vacancy_id} не найдена")
        return dict(row)

    def get_vacancy(self, vacancy_id):
        with self._db(self._inspect()) as conn:
            return self._vacancy(conn, vacancy_id)

    def draft_letter(self, vacancy_id, template=None):
        if template is not None and (not isinstance(template, str) or len(template) > 20000):
            raise CVError("template: требуется текст не длиннее 20000 символов")
        info = self._inspect()
        with self._db(info) as conn:
            vacancy = self._vacancy(conn, vacancy_id)
        letter = render_letter(info["cover_letter"] if template is None else template, vacancy)
        return {"vacancy_id": vacancy_id, "letter": letter, "sent": False,
                "unresolved_placeholders": re.findall(r"\{\w+\}", letter)}

    def score_vacancy(self, vacancy):
        if not isinstance(vacancy, dict) or not isinstance(vacancy.get("title"), str):
            raise CVError("vacancy.title: требуется строка")
        if len(json.dumps(vacancy)) > 200000:
            raise CVError("Текст вакансии слишком длинный")
        return self._bridge("score", {"vacancy": vacancy})

    def mark_vacancy(self, vacancy_id, status, note=""):
        if status not in ("applied", "skipped", "new"):
            raise CVError("status: applied, skipped или new")
        if not isinstance(note, str) or len(note) > 4000:
            raise CVError("note: требуется текст не длиннее 4000 символов")
        with self._db(self._inspect(), writable=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = self._vacancy(conn, vacancy_id)
            if old["status"] in TERMINAL:
                if status != "applied":
                    raise CVError("Уже отправленный отклик нельзя вернуть в очередь")
                return {"vacancy_id": vacancy_id, "status": old["status"], "changed": False, "sent": False}
            if old["status"] == status:
                return {"vacancy_id": vacancy_id, "status": status, "changed": False, "sent": False}
            if status == "applied":
                # Marking records the user's report; it never sends anything to hh.ru.
                conn.execute("INSERT INTO applications(vacancy_id,status,note,created_at) VALUES (?,?,?,?)",
                             (vacancy_id, status, note or "manual", dt.datetime.now().isoformat(timespec="seconds")))
            conn.execute("UPDATE vacancies SET status=?,note=? WHERE id=?", (status, note or "manual", vacancy_id))
            conn.commit()
        return {"vacancy_id": vacancy_id, "status": status, "changed": True, "sent": False}

    def prepare_run(self, action, limit=3, min_score=None, letters=None, vacancy_ids=None):
        if action not in ("login", "search", "check", "apply"):
            raise CVError("action: login, search, check или apply")
        if letters is not None and action != "apply":
            raise CVError("Индивидуальные письма задаются только для apply")
        bounded_int(limit, "limit", 1, 25)
        if vacancy_ids is not None:
            if action not in ("apply", "check") or not isinstance(vacancy_ids, list) or not vacancy_ids:
                raise CVError("vacancy_ids: непустой список ID только для apply/check")
            for vid in vacancy_ids:
                bounded_int(vid, "vacancy_id", 1, 2**63 - 1)
            if len(set(vacancy_ids)) != len(vacancy_ids):
                raise CVError("vacancy_ids: повторяющиеся ID")
            if len(vacancy_ids) > limit:
                raise CVError("Количество выбранных вакансий превышает limit")
        info = self._inspect()
        minimum = info["min_score"] if min_score is None else bounded_int(min_score, "min_score", 0, 200)
        vacancies = []
        if action in ("check", "apply"):
            with self._db(info) as conn:
                quota = max(0, info["max_per_day"] - self._stats(conn)["applied_today"])
                effective = min(limit, info["max_per_run"], quota)
                if vacancy_ids is not None:
                    if len(vacancy_ids) > effective:
                        raise CVError("Выбранные вакансии превышают квоту или max_per_run")
                    vacancies = [self._vacancy(conn, vid) for vid in vacancy_ids]
                    if any(v["status"] not in ELIGIBLE or v["score"] < minimum for v in vacancies):
                        raise CVError("Выбрана вакансия вне очереди new/checked или ниже порога скора")
                else:
                    rows = conn.execute(
                        "SELECT * FROM vacancies WHERE status IN ('new','checked') AND score>=? "
                        "ORDER BY score DESC,id DESC LIMIT ?", (minimum, effective))
                    vacancies = [dict(row) for row in rows]
            if not vacancies:
                raise CVError("Нет подходящих вакансий или исчерпан дневной лимит. Запуск не подготовлен.")
        planned_letters = [{"vacancy_id": v["id"], "letter": render_letter(info["cover_letter"], v)} for v in vacancies]
        if letters is not None:
            if not isinstance(letters, list) or len(letters) != len(vacancies):
                raise CVError("Передайте ровно одно письмо для каждой выбранной вакансии")
            supplied = {}
            for entry in letters:
                if not isinstance(entry, dict) or set(entry) != {"vacancy_id", "letter"}:
                    raise CVError("Формат letters: [{vacancy_id, letter}]")
                bounded_int(entry["vacancy_id"], "vacancy_id", 1, 2**63 - 1)
                if entry["vacancy_id"] in supplied or not isinstance(entry["letter"], str) or len(entry["letter"]) > 20000:
                    raise CVError("Недопустимое или повторяющееся письмо")
                supplied[entry["vacancy_id"]] = entry["letter"]
            if set(supplied) != {v["id"] for v in vacancies}:
                raise CVError("Список писем не совпадает с выбранными вакансиями. Проверьте очередь.")
            planned_letters = [{"vacancy_id": v["id"], "letter": supplied[v["id"]]} for v in vacancies]
        if action == "apply" and any(re.search(r"\{\w+\}", item["letter"]) for item in planned_letters):
            raise CVError("В письме остались незаполненные шаблоны. Исправьте письмо перед отправкой.")
        plan_id = uuid.uuid4().hex
        argv = [sys.executable, str(ROOT / "scripts/run_action.py"), plan_id, "--allow-browser"]
        if action == "apply":
            argv.append("--allow-send")
        now = dt.datetime.now(dt.timezone.utc)
        plan = {"plan_id": plan_id, "action": action, "status": "prepared", "created_at": now.isoformat(),
                "expires_at": (now + dt.timedelta(hours=1)).isoformat(),
                "workspace": info["workspace"], "config_path": info["config_path"],
                "python": info["python"], "db_path": info["db_path"],
                "config_sha256": info["config_sha256"], "source_sha256": info["source_sha256"],
                "vacancies": vacancies, "vacancies_sha256": fingerprint(vacancies),
                "limit": len(vacancies) if vacancies else limit, "min_score": minimum,
                "letters": planned_letters,
                "argv": argv, "command": shlex.join(argv),
                "runner_environment": {"VB_CV_BOT_HOME": str(self.home)},
                "effects": {"starts_browser": True, "sends_applications": action == "apply",
                            "writes_source_database": action != "login", "reads_browser_session": True,
                            "notifications": False, "screenshots": False},
                "approval_required": "Явное разрешение на этот запуск браузера" +
                    (" и отправку откликов на перечисленные вакансии" if action == "apply" else ""),
                "delivery_caveat": "Backend hhbot может отправить отклик первым кликом без формы и письма. "
                    "Статус applied отражает подтверждение интерфейса, а не независимую проверку доставки.",
                "browser_started": False, "browser_start_attempted": False}
        atomic_json(self.home / "plans" / (plan_id + ".json"), plan)
        return plan

    def get_run(self, plan_id):
        if not isinstance(plan_id, str) or not re.fullmatch(r"[0-9a-f]{32}", plan_id):
            raise CVError("Некорректный plan_id")
        path = self.home / "plans" / (plan_id + ".json")
        if not path.is_file():
            raise CVError("План запуска не найден")
        return json.loads(path.read_text(encoding="utf-8"))

    def validate_plan(self, plan):
        if plan["status"] != "prepared":
            raise CVError("План уже использован. Повторный запуск требует нового preview и разрешения.")
        if dt.datetime.now(dt.timezone.utc) >= dt.datetime.fromisoformat(plan["expires_at"]):
            raise CVError("План устарел. Подготовьте новый запуск.")
        info = self._inspect()
        for key in ("workspace", "config_path", "python", "db_path", "config_sha256", "source_sha256"):
            if info[key] != plan[key]:
                raise CVError("Проект или конфиг изменился после preview. Подготовьте новый запуск.")
        if plan["action"] in ("apply", "check"):
            with self._db(info) as conn:
                rows = [self._vacancy(conn, v["id"]) for v in plan["vacancies"]]
                if fingerprint(rows) != plan["vacancies_sha256"]:
                    raise CVError("Вакансии изменились после preview. Подготовьте новый запуск.")
                if self._stats(conn)["applied_today"] + len(rows) > info["max_per_day"]:
                    raise CVError("Изменился дневной лимит. Подготовьте новый запуск.")
        return info
