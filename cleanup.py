#!/usr/bin/env python3
"""
Orthanc PACS Cleanup — знаходить старі дослідження та видаляє їх після погодження в GLPI.

Команди:
  gather  — зібрати список досліджень та створити тікет у GLPI
  check   — перевірити статус тікету та видалити якщо погоджено
  delete  — ручне видалення з підтвердженням (без GLPI)
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import urllib3
import requests
from dotenv import load_dotenv

# ── Конфігурація ──────────────────────────────────────────────────────────────

def cfg(key, default=None):
    val = os.getenv(key, default)
    if val is None:
        print(f"[ERROR] Змінна {key} не задана в .env", file=sys.stderr)
        sys.exit(1)
    return val


def load_config():
    c = {
        "orthanc_url":         cfg("ORTHANC_URL", "http://localhost:8042"),
        "orthanc_user":        cfg("ORTHANC_USER", "orthanc"),
        "orthanc_password":    cfg("ORTHANC_PASSWORD", "orthanc"),
        "orthanc_verify_ssl":  cfg("ORTHANC_VERIFY_SSL", "false").lower() == "true",
        "retention_years":     int(cfg("RETENTION_YEARS", "5")),
        "glpi_url":            cfg("GLPI_URL", "http://localhost:8080"),
        "glpi_app_token":      cfg("GLPI_APP_TOKEN"),
        "glpi_user_token":     cfg("GLPI_USER_TOKEN"),
        "glpi_category_id":    int(cfg("GLPI_CATEGORY_ID", "1")),
        "glpi_entity_id":      int(cfg("GLPI_ENTITY_ID", "0")),
        "glpi_assign_user_id": int(cfg("GLPI_ASSIGN_USER_ID", "4")),
        "glpi_verify_ssl":     cfg("GLPI_VERIFY_SSL", "false").lower() == "true",
        "studies_file":        Path(cfg("STUDIES_FILE", "/tmp/orthanc-cleanup/files/studies_to_delete.json")),
        "state_file":          Path(cfg("STATE_FILE", "/tmp/orthanc-cleanup/state.json")),
    }
    if not c["orthanc_verify_ssl"] or not c["glpi_verify_ssl"]:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return c


# ── Orthanc ───────────────────────────────────────────────────────────────────

def date_threshold(retention_years):
    now = datetime.now()
    try:
        return now.replace(year=now.year - retention_years)
    except ValueError:  # 29 лютого у невисокосний рік
        return now.replace(year=now.year - retention_years, day=28)


def format_size(size_bytes):
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} ПБ"


def fetch_old_studies(c):
    threshold = date_threshold(c["retention_years"]).strftime("%Y%m%d")
    print(f"Шукаємо дослідження старші {c['retention_years']} років (до {threshold})...")

    # Отримуємо тільки список ID — без Expand, щоб не завантажувати всі метадані одразу
    resp = requests.post(
        f"{c['orthanc_url']}/tools/find",
        auth=(c["orthanc_user"], c["orthanc_password"]),
        verify=c["orthanc_verify_ssl"],
        timeout=30,
        json={
            "Level": "Study",
            "Expand": False,
            "Query": {"StudyDate": f"19000101-{threshold}"},
        },
    )
    resp.raise_for_status()
    ids = resp.json()
    print(f"Знайдено {len(ids)} досліджень, завантажуємо метадані...")

    studies = []
    for study_id in ids:
        r = requests.get(
            f"{c['orthanc_url']}/studies/{study_id}",
            auth=(c["orthanc_user"], c["orthanc_password"]),
            verify=c["orthanc_verify_ssl"],
            timeout=10,
        )
        r.raise_for_status()
        s = r.json()
        studies.append({
            "orthanc_id":   study_id,
            "patient_name": s.get("PatientMainDicomTags", {}).get("PatientName", "N/A"),
            "patient_id":   s.get("PatientMainDicomTags", {}).get("PatientID", "N/A"),
            "study_date":   s.get("MainDicomTags", {}).get("StudyDate", "N/A"),
            "description":  s.get("MainDicomTags", {}).get("StudyDescription", "N/A"),
        })
    return studies


def delete_studies(c, studies):
    deleted, skipped, failed, freed_bytes = 0, 0, [], 0
    for s in studies:
        size = 0
        try:
            r = requests.get(
                f"{c['orthanc_url']}/studies/{s['orthanc_id']}/statistics",
                auth=(c["orthanc_user"], c["orthanc_password"]),
                verify=c["orthanc_verify_ssl"],
                timeout=10,
            )
            if r.ok:
                size = r.json().get("DiskSize", 0)
        except Exception:
            pass

        resp = requests.delete(
            f"{c['orthanc_url']}/studies/{s['orthanc_id']}",
            auth=(c["orthanc_user"], c["orthanc_password"]),
            verify=c["orthanc_verify_ssl"],
            timeout=(10, None),  # з'єднання 10с, читання без ліміту — видалення 1M файлів може тривати годинами
        )
        if resp.status_code == 200:
            freed_bytes += size
            size_str = format_size(size) if size else "розмір невідомий"
            print(f"  ✓ {s['study_date']} | {s['patient_name']} ({size_str})")
            deleted += 1
        elif resp.status_code == 404:
            print(f"  ~ {s['study_date']} | {s['patient_name']} (вже видалено)")
            skipped += 1
        else:
            print(f"  ✗ {s['study_date']} | {s['patient_name']} (HTTP {resp.status_code})", file=sys.stderr)
            failed.append(s)
    return deleted, skipped, failed, freed_bytes


# ── GLPI ──────────────────────────────────────────────────────────────────────

class GlpiSession:
    def __init__(self, c):
        self.c = c
        self.token = None

    def __enter__(self):
        resp = requests.get(
            f"{self.c['glpi_url']}/apirest.php/initSession",
            headers={
                "App-Token": self.c["glpi_app_token"],
                "Authorization": f"user_token {self.c['glpi_user_token']}",
            },
            verify=self.c["glpi_verify_ssl"],
        )
        resp.raise_for_status()
        self.token = resp.json()["session_token"]
        return self

    def __exit__(self, *_):
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

    def _headers(self):
        return {"App-Token": self.c["glpi_app_token"], "Session-Token": self.token}

    def create_ticket(self, studies):
        c = self.c
        rows = "\n".join(
            f"<tr><td>{s['study_date']}</td><td>{s['patient_name']}</td>"
            f"<td>{s['patient_id']}</td><td>{s['description']}</td>"
            f"<td><code>{s['orthanc_id']}</code></td></tr>"
            for s in sorted(studies, key=lambda x: x["study_date"])
        )
        content = (
            f"<h3>Видалення досліджень старших {c['retention_years']} років з Orthanc PACS</h3>"
            f"<p>Сервер: <strong>{c['server_name']}</strong></p>"
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
            json={"input": {
                "name": f"Видалення досліджень Orthanc >{c['retention_years']} років | {c['server_name']} | {datetime.now().strftime('%Y-%m-%d')}",
                "content": content,
                "type": 2, "status": 1, "urgency": 2, "impact": 2, "priority": 2,
                "itilcategories_id": c["glpi_category_id"],
                "entities_id":       c["glpi_entity_id"],
                "_users_id_assign":  c["glpi_assign_user_id"],
            }},
        )
        resp.raise_for_status()
        return resp.json()["id"]

    def get_ticket_status(self, ticket_id):
        resp = requests.get(
            f"{self.c['glpi_url']}/apirest.php/Ticket/{ticket_id}",
            headers=self._headers(),
            verify=self.c["glpi_verify_ssl"],
        )
        resp.raise_for_status()
        return resp.json()["status"]

    def add_comment(self, ticket_id, deleted, skipped, freed_bytes):
        resp = requests.post(
            f"{self.c['glpi_url']}/apirest.php/ITILFollowup",
            headers=self._headers(),
            verify=self.c["glpi_verify_ssl"],
            json={"input": {
                "itemtype":  "Ticket",
                "items_id":  ticket_id,
                "content": (
                    f"Видалення виконано автоматично {datetime.now().strftime('%Y-%m-%d %H:%M')}. "
                    f"Видалено: {deleted}, вже відсутніх: {skipped}. "
                    f"Звільнено: {format_size(freed_bytes)}."
                ),
                "is_private": 0,
            }},
        )
        if not resp.ok:
            print(f"[WARNING] Не вдалося додати коментар до тікету #{ticket_id}: HTTP {resp.status_code}", file=sys.stderr)


# ── Команди ───────────────────────────────────────────────────────────────────

def cmd_gather(c):
    if c["state_file"].exists():
        print(f"[ERROR] {c['state_file']} вже існує — активний тікет ще не погоджено.", file=sys.stderr)
        print("Дочекайтеся погодження і запуску 'check', або видаліть файл вручну.", file=sys.stderr)
        sys.exit(1)

    studies = fetch_old_studies(c)
    if not studies:
        print("Досліджень для видалення не знайдено.")
        return

    print(f"Знайдено {len(studies)} досліджень:")
    for s in sorted(studies, key=lambda x: x["study_date"]):
        print(f"  {s['study_date']} | {s['patient_name']:<35} | {s['patient_id']:<10} | {s['description']}")

    c["studies_file"].parent.mkdir(parents=True, exist_ok=True)
    c["state_file"].parent.mkdir(parents=True, exist_ok=True)
    c["studies_file"].write_text(json.dumps(studies, ensure_ascii=False, indent=2))

    with GlpiSession(c) as glpi:
        ticket_id = glpi.create_ticket(studies)
        print(f"Тікет #{ticket_id} створено → {c['glpi_url']}/front/ticket.form.php?id={ticket_id}")

    c["state_file"].write_text(json.dumps(
        {"ticket_id": ticket_id, "created_at": datetime.now().strftime("%Y-%m-%d")},
        indent=2,
    ))
    print(f"Стан збережено: {c['state_file']}")


def cmd_check(c):
    if not c["state_file"].exists():
        print("Активного тікету немає — нічого робити.")
        return

    try:
        state = json.loads(c["state_file"].read_text())
        ticket_id = state["ticket_id"]
    except json.JSONDecodeError:
        print(f"[ERROR] {c['state_file']} пошкоджений. Видаліть файл та запустіть gather знову.", file=sys.stderr)
        sys.exit(1)
    except KeyError:
        print(f"[ERROR] {c['state_file']} не містить ticket_id. Видаліть файл та запустіть gather знову.", file=sys.stderr)
        sys.exit(1)
    print(f"Перевіряємо тікет #{ticket_id} (створено {state['created_at']})...")

    # Статуси GLPI: 1=New, 2=Processing(assigned), 3=Processing(planned),
    #               4=Pending, 5=Solved, 6=Closed
    with GlpiSession(c) as glpi:
        status = glpi.get_ticket_status(ticket_id)
        if status not in (5, 6):
            print(f"Тікет ще не погоджено (статус={status}). Очікуємо Solved(5) або Closed(6).")
            return

        if not c["studies_file"].exists():
            print(f"[ERROR] Тікет погоджено, але {c['studies_file']} відсутній.", file=sys.stderr)
            sys.exit(1)

        try:
            studies = json.loads(c["studies_file"].read_text())
        except json.JSONDecodeError:
            print(f"[ERROR] {c['studies_file']} пошкоджений.", file=sys.stderr)
            sys.exit(1)
        print(f"Тікет погоджено. Видаляємо {len(studies)} досліджень...")
        deleted, skipped, failed, freed = delete_studies(c, studies)
        glpi.add_comment(ticket_id, deleted, skipped, freed)

    c["studies_file"].unlink()
    c["state_file"].unlink()
    print(f"Готово. Видалено: {deleted}, пропущено: {skipped}. Звільнено: {format_size(freed)}.")
    if failed:
        print(f"[WARNING] Не вдалося видалити {len(failed)} досліджень:", file=sys.stderr)
        for s in failed:
            print(f"  {s['study_date']} | {s['patient_name']} | {s['orthanc_id']}", file=sys.stderr)


def cmd_delete(c):
    if not c["studies_file"].exists():
        print(f"[ERROR] {c['studies_file']} не знайдено. Спочатку запустіть gather.", file=sys.stderr)
        sys.exit(1)

    try:
        studies = json.loads(c["studies_file"].read_text())
    except json.JSONDecodeError:
        print(f"[ERROR] {c['studies_file']} пошкоджений.", file=sys.stderr)
        sys.exit(1)
    print(f"Буде видалено {len(studies)} досліджень:")
    for s in studies:
        print(f"  {s['study_date']} | {s['patient_name']} | {s['orthanc_id']}")

    confirm = input("\nВидалити? Введіть 'yes': ")
    if confirm.strip() != "yes":
        print("Скасовано.")
        return

    deleted, skipped, failed, freed = delete_studies(c, studies)
    c["studies_file"].unlink()
    if c["state_file"].exists():
        c["state_file"].unlink()
    print(f"Готово. Видалено: {deleted}, пропущено: {skipped}. Звільнено: {format_size(freed)}.")
    if failed:
        print(f"[WARNING] Не вдалося видалити {len(failed)} досліджень:", file=sys.stderr)
        for s in failed:
            print(f"  {s['study_date']} | {s['patient_name']} | {s['orthanc_id']}", file=sys.stderr)


# ── Точка входу ───────────────────────────────────────────────────────────────

COMMANDS = {"gather": cmd_gather, "check": cmd_check, "delete": cmd_delete}

if __name__ == "__main__":
    args = sys.argv[1:]

    # Витягти --env path/to/file.env якщо вказано
    env_file = ".env"
    if "--env" in args:
        idx = args.index("--env")
        if idx + 1 >= len(args):
            print("[ERROR] --env потребує шлях до файлу", file=sys.stderr)
            sys.exit(1)
        env_file = args.pop(idx + 1)
        args.pop(idx)

    if len(args) != 1 or args[0] not in COMMANDS:
        print(f"Використання: python cleanup.py [{' | '.join(COMMANDS)}] [--env path/to/.env]")
        sys.exit(1)

    if not os.path.exists(env_file):
        print(f"[ERROR] Файл конфігурації не знайдено: {env_file}", file=sys.stderr)
        sys.exit(1)
    load_dotenv(env_file, override=True)
    config = load_config()
    # витягуємо назву сервера з імені файлу: server.xr.env → xr
    basename = os.path.splitext(os.path.basename(env_file))[0]
    config["server_name"] = basename[len("server."):] if basename.startswith("server.") else basename
    try:
        COMMANDS[args[0]](config)
    except requests.HTTPError as e:
        print(f"[HTTP ERROR] {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nПерервано.")
        sys.exit(1)
