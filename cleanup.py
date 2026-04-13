#!/usr/bin/env python3
"""
Orthanc PACS Cleanup — знаходить старі дослідження та видаляє їх після погодження в GLPI.

Команди:
  gather  — зібрати список досліджень та створити тікет у GLPI
  check   — перевірити статус тікету та видалити якщо погоджено
  delete  — ручне видалення з підтвердженням (без GLPI)
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import urllib3
import requests
from dotenv import load_dotenv

# ── Логування ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ── Константи ─────────────────────────────────────────────────────────────────

GLPI_STATUS_SOLVED = 5
GLPI_STATUS_CLOSED = 6
GLPI_APPROVED_STATUSES = (GLPI_STATUS_SOLVED, GLPI_STATUS_CLOSED)

GLPI_TIMEOUT = (10, 60)  # (connect, read) секунд


# ── Конфігурація ──────────────────────────────────────────────────────────────

def cfg(key: str, default: str | None = None) -> str:
    val = os.getenv(key, default)
    if val is None:
        log.error("Змінна %s не задана в .env", key)
        sys.exit(1)
    return val


def _parse_verify(raw: str) -> bool | str:
    """false → False, true → True, інше → шлях до CA-файлу."""
    low = raw.strip().lower()
    if low == "false":
        return False
    if low == "true":
        return True
    if not os.path.exists(raw):
        log.error("VERIFY_SSL: CA-файл не знайдено: %s", raw)
        sys.exit(1)
    log.info("SSL: використовується CA-файл %s", raw)
    return raw


def _int_cfg(key: str, default: str) -> int:
    raw = cfg(key, default)
    try:
        return int(raw)
    except ValueError:
        log.error("Змінна %s має бути цілим числом, отримано: %r", key, raw)
        sys.exit(1)


def load_config() -> dict:
    orthanc_verify_ssl = _parse_verify(cfg("ORTHANC_VERIFY_SSL", "false"))
    glpi_verify_ssl    = _parse_verify(cfg("GLPI_VERIFY_SSL",    "false"))

    if orthanc_verify_ssl is False:
        log.warning("ORTHANC_VERIFY_SSL=false — SSL-перевірка вимкнена (небезпечно для production)")
    if glpi_verify_ssl is False:
        log.warning("GLPI_VERIFY_SSL=false — SSL-перевірка вимкнена (небезпечно для production)")
    if orthanc_verify_ssl is False or glpi_verify_ssl is False:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    retention_years = _int_cfg("RETENTION_YEARS", "5")
    if retention_years < 1:
        log.error("RETENTION_YEARS має бути >= 1, отримано: %d", retention_years)
        sys.exit(1)

    orthanc_url = cfg("ORTHANC_URL", "http://localhost:8042")
    if orthanc_url.startswith("http://") and "localhost" not in orthanc_url and "127.0.0.1" not in orthanc_url:
        log.warning("ORTHANC_URL використовує HTTP без шифрування — паролі передаються відкрито")

    return {
        "orthanc_url":         orthanc_url,
        "orthanc_user":        cfg("ORTHANC_USER", "orthanc"),
        "orthanc_password":    cfg("ORTHANC_PASSWORD", "orthanc"),
        "orthanc_verify_ssl":  orthanc_verify_ssl,
        "retention_years":     retention_years,
        "glpi_url":            cfg("GLPI_URL", "http://localhost:8080"),
        "glpi_app_token":      cfg("GLPI_APP_TOKEN"),
        "glpi_user_token":     cfg("GLPI_USER_TOKEN"),
        "glpi_category_id":    _int_cfg("GLPI_CATEGORY_ID", "1"),
        "glpi_entity_id":      _int_cfg("GLPI_ENTITY_ID", "0"),
        "glpi_assign_user_id": _int_cfg("GLPI_ASSIGN_USER_ID", "4"),
        "glpi_verify_ssl":     glpi_verify_ssl,
        "studies_file":        Path(cfg("STUDIES_FILE", "/tmp/orthanc-cleanup/files/studies_to_delete.json")),
        "state_file":          Path(cfg("STATE_FILE", "/tmp/orthanc-cleanup/state.json")),
        "session":             requests.Session(),
    }


# ── Утиліти ───────────────────────────────────────────────────────────────────

def format_size(size_bytes: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} ПБ"


def _atomic_write(path: Path, data: str) -> None:
    """Записує файл атомарно через тимчасовий файл (os.replace)."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


# ── Orthanc ───────────────────────────────────────────────────────────────────

def date_threshold(retention_years: int) -> datetime:
    now = datetime.now()
    try:
        threshold = now.replace(year=now.year - retention_years)
    except ValueError:  # 29 лютого у невисокосний рік
        threshold = now.replace(year=now.year - retention_years, day=28)
    # -1 день: дослідження рівно N-річної давності не включаємо
    return threshold - timedelta(days=1)


def fetch_old_studies(c: dict) -> list[dict]:
    threshold = date_threshold(c["retention_years"]).strftime("%Y%m%d")
    log.info("Шукаємо дослідження старші %d років (до %s)...", c["retention_years"], threshold)

    # Expand: True — отримуємо всі метадані одним запитом (уникаємо N+1)
    resp = c["session"].post(
        f"{c['orthanc_url']}/tools/find",
        auth=(c["orthanc_user"], c["orthanc_password"]),
        verify=c["orthanc_verify_ssl"],
        timeout=60,
        json={
            "Level":  "Study",
            "Expand": True,
            "Query":  {"StudyDate": f"19000101-{threshold}"},
        },
    )
    resp.raise_for_status()
    raw = resp.json()
    log.info("Знайдено %d досліджень.", len(raw))

    return [
        {
            "orthanc_id":   s["ID"],
            "patient_name": s.get("PatientMainDicomTags", {}).get("PatientName", ""),
            "patient_id":   s.get("PatientMainDicomTags", {}).get("PatientID", ""),
            "study_date":   s.get("MainDicomTags", {}).get("StudyDate", ""),
            "description":  s.get("MainDicomTags", {}).get("StudyDescription", ""),
        }
        for s in raw
    ]


def delete_studies(c: dict, studies: list[dict]) -> tuple[int, int, list[dict], int]:
    deleted, skipped, failed, freed_bytes = 0, 0, [], 0

    for s in studies:
        # Отримати розмір до видалення
        size = 0
        try:
            r = c["session"].get(
                f"{c['orthanc_url']}/studies/{s['orthanc_id']}/statistics",
                auth=(c["orthanc_user"], c["orthanc_password"]),
                verify=c["orthanc_verify_ssl"],
                timeout=10,
            )
            if r.ok:
                size = r.json().get("DiskSize", 0)
        except Exception:
            pass

        # Видалення з обробкою мережевих помилок
        try:
            resp = c["session"].delete(
                f"{c['orthanc_url']}/studies/{s['orthanc_id']}",
                auth=(c["orthanc_user"], c["orthanc_password"]),
                verify=c["orthanc_verify_ssl"],
                timeout=(10, None),  # читання без ліміту — видалення великих архівів може тривати годинами
            )
        except requests.RequestException as e:
            log.error("  ✗ %s | %s — мережева помилка: %s", s["study_date"], s["patient_name"], e)
            failed.append(s)
            continue

        if resp.status_code == 200:
            freed_bytes += size
            size_str = format_size(size) if size else "розмір невідомий"
            log.info("  ✓ %s | %s (%s)", s["study_date"], s["patient_name"], size_str)
            deleted += 1
        elif resp.status_code == 404:
            log.info("  ~ %s | %s (вже видалено)", s["study_date"], s["patient_name"])
            skipped += 1
        else:
            log.error("  ✗ %s | %s (HTTP %d)", s["study_date"], s["patient_name"], resp.status_code)
            failed.append(s)

    return deleted, skipped, failed, freed_bytes


# ── GLPI ──────────────────────────────────────────────────────────────────────

class GlpiSession:
    def __init__(self, c: dict) -> None:
        self.c = c
        self.token: str | None = None

    def __enter__(self) -> "GlpiSession":
        resp = requests.get(
            f"{self.c['glpi_url']}/apirest.php/initSession",
            headers={
                "App-Token":     self.c["glpi_app_token"],
                "Authorization": f"user_token {self.c['glpi_user_token']}",
            },
            verify=self.c["glpi_verify_ssl"],
            timeout=GLPI_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if "session_token" not in data:
            raise ValueError(f"GLPI initSession: відсутній session_token у відповіді: {data}")
        self.token = data["session_token"]
        return self

    def __exit__(self, *_) -> None:
        if not self.token:
            return
        try:
            requests.get(
                f"{self.c['glpi_url']}/apirest.php/killSession",
                headers={"App-Token": self.c["glpi_app_token"], "Session-Token": self.token},
                verify=self.c["glpi_verify_ssl"],
                timeout=10,
            )
        except Exception:
            pass

    def _headers(self) -> dict:
        return {"App-Token": self.c["glpi_app_token"], "Session-Token": self.token}

    def create_ticket(self, studies: list[dict]) -> int:
        c = self.c
        # html.escape() захищає від XSS — DICOM-поля можуть містити <, >, & тощо
        rows = "\n".join(
            f"<tr>"
            f"<td>{html.escape(s['study_date'])}</td>"
            f"<td>{html.escape(s['patient_name'])}</td>"
            f"<td>{html.escape(s['patient_id'])}</td>"
            f"<td>{html.escape(s['description'])}</td>"
            f"<td><code>{html.escape(s['orthanc_id'])}</code></td>"
            f"</tr>"
            for s in sorted(studies, key=lambda x: x["study_date"])
        )
        content = (
            f"<h3>Видалення досліджень старших {c['retention_years']} років з Orthanc PACS</h3>"
            f"<p>Сервер: <strong>{html.escape(c['server_name'])}</strong></p>"
            f"<p>Дата формування: {datetime.now().strftime('%Y-%m-%d')}</p>"
            f"<p>Кількість досліджень: <strong>{len(studies)}</strong></p>"
            f"<table border='1' cellpadding='4' cellspacing='0'>"
            f"<thead><tr><th>Дата</th><th>Пацієнт</th><th>PatientID</th><th>Опис</th><th>Orthanc ID</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            f"<p><em>Після погодження (статус Solved/Closed) видалення запуститься автоматично.</em></p>"
        )
        resp = requests.post(
            f"{c['glpi_url']}/apirest.php/Ticket",
            headers=self._headers(),
            verify=c["glpi_verify_ssl"],
            timeout=GLPI_TIMEOUT,
            json={"input": {
                "name": (
                    f"Видалення досліджень Orthanc >{c['retention_years']} років"
                    f" | {html.escape(c['server_name'])}"
                    f" | {datetime.now().strftime('%Y-%m-%d')}"
                ),
                "content":           content,
                "type":              2,
                "status":            1,
                "urgency":           2,
                "impact":            2,
                "priority":          2,
                "itilcategories_id": c["glpi_category_id"],
                "entities_id":       c["glpi_entity_id"],
                "_users_id_assign":  c["glpi_assign_user_id"],
            }},
        )
        resp.raise_for_status()
        data = resp.json()
        if "id" not in data:
            raise ValueError(f"GLPI create ticket: відсутній id у відповіді: {data}")
        return data["id"]

    def get_ticket_status(self, ticket_id: int) -> int:
        resp = requests.get(
            f"{self.c['glpi_url']}/apirest.php/Ticket/{ticket_id}",
            headers=self._headers(),
            verify=self.c["glpi_verify_ssl"],
            timeout=GLPI_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if "status" not in data:
            raise ValueError(f"GLPI get ticket: відсутній status у відповіді: {data}")
        return data["status"]

    def add_comment(self, ticket_id: int, deleted: int, skipped: int, freed_bytes: int) -> None:
        resp = requests.post(
            f"{self.c['glpi_url']}/apirest.php/ITILFollowup",
            headers=self._headers(),
            verify=self.c["glpi_verify_ssl"],
            timeout=GLPI_TIMEOUT,
            json={"input": {
                "itemtype": "Ticket",
                "items_id": ticket_id,
                "content": (
                    f"Видалення виконано автоматично {datetime.now().strftime('%Y-%m-%d %H:%M')}. "
                    f"Видалено: {deleted}, вже відсутніх: {skipped}. "
                    f"Звільнено: {format_size(freed_bytes)}."
                ),
                "is_private": 0,
            }},
        )
        if not resp.ok:
            log.warning("Не вдалося додати коментар до тікету #%d: HTTP %d", ticket_id, resp.status_code)


# ── Команди ───────────────────────────────────────────────────────────────────

def cmd_gather(c: dict) -> None:
    if c["state_file"].exists():
        log.error("%s вже існує — активний тікет ще не погоджено.", c["state_file"])
        log.error("Дочекайтеся погодження і запуску 'check', або видаліть файл вручну.")
        sys.exit(1)

    studies = fetch_old_studies(c)
    if not studies:
        log.info("Досліджень для видалення не знайдено.")
        return

    log.info("Знайдено %d досліджень:", len(studies))
    for s in sorted(studies, key=lambda x: x["study_date"]):
        log.info("  %s | %-35s | %-10s | %s",
                 s["study_date"], s["patient_name"], s["patient_id"], s["description"])

    c["studies_file"].parent.mkdir(parents=True, exist_ok=True)
    c["state_file"].parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(c["studies_file"], json.dumps(studies, ensure_ascii=False, indent=2))

    with GlpiSession(c) as glpi:
        ticket_id = glpi.create_ticket(studies)
        log.info("Тікет #%d створено → %s/front/ticket.form.php?id=%d",
                 ticket_id, c["glpi_url"], ticket_id)
        # Записуємо state всередині with-блоку одразу після отримання ticket_id —
        # щоб уникнути дублювання тікетів при аварійному завершенні процесу
        _atomic_write(c["state_file"], json.dumps(
            {"ticket_id": ticket_id, "created_at": datetime.now().strftime("%Y-%m-%d")},
            indent=2,
        ))

    log.info("Стан збережено: %s", c["state_file"])


def cmd_check(c: dict) -> None:
    if not c["state_file"].exists():
        log.info("Активного тікету немає — нічого робити.")
        return

    try:
        state = json.loads(c["state_file"].read_text())
        ticket_id = state["ticket_id"]
    except json.JSONDecodeError:
        log.error("%s пошкоджений. Видаліть файл та запустіть gather знову.", c["state_file"])
        sys.exit(1)
    except KeyError:
        log.error("%s не містить ticket_id. Видаліть файл та запустіть gather знову.", c["state_file"])
        sys.exit(1)

    log.info("Перевіряємо тікет #%d (створено %s)...", ticket_id, state["created_at"])

    # Статуси GLPI: 1=New, 2=Processing(assigned), 3=Processing(planned),
    #               4=Pending, 5=Solved, 6=Closed
    with GlpiSession(c) as glpi:
        status = glpi.get_ticket_status(ticket_id)
        if status not in GLPI_APPROVED_STATUSES:
            log.info("Тікет ще не погоджено (статус=%d). Очікуємо Solved(%d) або Closed(%d).",
                     status, GLPI_STATUS_SOLVED, GLPI_STATUS_CLOSED)
            return

        if not c["studies_file"].exists():
            log.error("Тікет погоджено, але %s відсутній.", c["studies_file"])
            sys.exit(1)

        try:
            studies = json.loads(c["studies_file"].read_text())
        except json.JSONDecodeError:
            log.error("%s пошкоджений.", c["studies_file"])
            sys.exit(1)

        log.info("Тікет погоджено. Видаляємо %d досліджень...", len(studies))
        deleted, skipped, failed, freed = delete_studies(c, studies)
        glpi.add_comment(ticket_id, deleted, skipped, freed)

    c["studies_file"].unlink()
    c["state_file"].unlink()
    log.info("Готово. Видалено: %d, пропущено: %d. Звільнено: %s.", deleted, skipped, format_size(freed))

    if failed:
        failed_file = c["studies_file"].with_name("failed_studies.json")
        _atomic_write(failed_file, json.dumps(failed, ensure_ascii=False, indent=2))
        log.warning("Не вдалося видалити %d досліджень — збережено у %s", len(failed), failed_file)


def cmd_delete(c: dict) -> None:
    if not c["studies_file"].exists():
        log.error("%s не знайдено. Спочатку запустіть gather.", c["studies_file"])
        sys.exit(1)

    try:
        studies = json.loads(c["studies_file"].read_text())
    except json.JSONDecodeError:
        log.error("%s пошкоджений.", c["studies_file"])
        sys.exit(1)

    log.info("Буде видалено %d досліджень:", len(studies))
    for s in studies:
        log.info("  %s | %s | %s", s["study_date"], s["patient_name"], s["orthanc_id"])

    if not sys.stdin.isatty():
        log.error("Команда 'delete' потребує інтерактивного терміналу.")
        sys.exit(1)

    try:
        confirm = input("\nВидалити? Введіть 'yes': ")
    except EOFError:
        log.error("Скасовано (stdin закрито).")
        sys.exit(1)

    if confirm.strip() != "yes":
        log.info("Скасовано.")
        return

    deleted, skipped, failed, freed = delete_studies(c, studies)
    c["studies_file"].unlink()
    if c["state_file"].exists():
        c["state_file"].unlink()
    log.info("Готово. Видалено: %d, пропущено: %d. Звільнено: %s.", deleted, skipped, format_size(freed))

    if failed:
        failed_file = c["studies_file"].with_name("failed_studies.json")
        _atomic_write(failed_file, json.dumps(failed, ensure_ascii=False, indent=2))
        log.warning("Не вдалося видалити %d досліджень — збережено у %s", len(failed), failed_file)


# ── Точка входу ───────────────────────────────────────────────────────────────

COMMANDS = {"gather": cmd_gather, "check": cmd_check, "delete": cmd_delete}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Orthanc PACS Cleanup — видалення старих досліджень з погодженням через GLPI."
    )
    parser.add_argument("command", choices=COMMANDS, help="Команда для виконання")
    parser.add_argument("--env", default=".env", metavar="FILE",
                        help="Шлях до .env файлу (default: .env)")
    args = parser.parse_args()

    if not os.path.exists(args.env):
        log.error("Файл конфігурації не знайдено: %s", args.env)
        sys.exit(1)

    load_dotenv(args.env, override=True)
    config = load_config()

    # Назва сервера з імені файлу: server.xr.env → xr
    stem = Path(args.env).stem  # "server.xr"
    config["server_name"] = stem[len("server."):] if stem.startswith("server.") else stem

    try:
        COMMANDS[args.command](config)
    except requests.HTTPError as e:
        log.error("HTTP ERROR: %s", e)
        sys.exit(1)
    except requests.RequestException as e:
        log.error("Мережева помилка: %s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        log.info("Перервано.")
        sys.exit(1)
    except Exception as e:
        log.error("Неочікувана помилка: %s", e, exc_info=True)
        sys.exit(1)
    finally:
        config["session"].close()


if __name__ == "__main__":
    main()
