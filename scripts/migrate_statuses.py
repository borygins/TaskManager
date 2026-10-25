"""Offline SQLite migration for unified client statuses and activity marks.

Run explicitly, with the application stopped:
    python scripts/migrate_statuses.py --database task_manager.sqlite3

The script is intentionally standalone: importing the application would open
the database before its schema has been checked and migrated.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import time
from uuid import uuid4


VERSION = "20261025_unified_client_statuses"
CLIENT_STATUSES = {
    "Новый", "Поиск контакта", "Переговоры", "Подписан", "Отказ", "Нет контактов",
}
STATUS_RENAMES = {"Согласие": "Переговоры"}
RESULT_TO_STATUS = {
    "Новый": "Новый",
    "Поиск контакта": "Поиск контакта",
    "Переговоры": "Переговоры",
    "Согласие": "Переговоры",
    "Подписан": "Подписан",
    "Договор подписан": "Подписан",
    "Договор запущен": "Подписан",
    "Отказ": "Отказ",
    "Нет контактов": "Нет контактов",
}
RESULT_TO_MARKS = {
    "Недозвон": ["missed_call"],
    "Перенос на дату": ["call_back"],
}
KNOWN_NEUTRAL_RESULTS = set(RESULT_TO_MARKS)
MARK_CODES = {
    "missed_call", "call_back", "meeting_scheduled", "thinking", "approval",
    "postponed", "proposal_sent", "waiting_documents", "documents_received",
}


def columns(connection: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {row[1]: row for row in connection.execute(f'PRAGMA table_info("{table}")')}


def check_integrity(connection: sqlite3.Connection) -> None:
    if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
        raise ValueError("SQLite integrity_check failed. Restore or repair the database first.")
    if connection.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("Foreign-key violations found. Migration stopped without changing data.")


def create_backup(path: Path, destination: Path) -> None:
    deadline = time.monotonic() + 60

    def progress(status, remaining, total):
        _ = (status, remaining, total)
        if time.monotonic() > deadline:
            raise TimeoutError("Backup took more than 60 seconds. Migration was not started.")

    with destination.open("xb"):
        pass
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target, pages=256, progress=progress)
            check_integrity(target)


def parsed_marks(raw: str, contact_id: int) -> list[str]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Contact #{contact_id} has invalid marks JSON.") from exc
    if not isinstance(value, list) or any(not isinstance(code, str) or code not in MARK_CODES for code in value):
        raise ValueError(f"Contact #{contact_id} has unknown activity marks.")
    if len(value) != len(set(value)):
        raise ValueError(f"Contact #{contact_id} has duplicate activity marks.")
    return value


def migrate(database: Path, backup_dir: Path | None = None) -> dict:
    path = database.resolve(strict=True)
    if not path.is_file():
        raise ValueError("Database must be an existing file.")
    result = {"already_current": False, "backup": None, "clients_updated": 0,
              "contacts_updated": 0, "previews_staled": 0}
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            client_columns = columns(db, "clients")
            contact_columns = columns(db, "contact_activities")
            if not client_columns or "status" not in client_columns:
                raise ValueError("Not a supported TaskManager database: clients.status is required.")
            if contact_columns and ("comment_id" not in contact_columns or contact_columns["funnel_id"][3]):
                raise ValueError(
                    "Run scripts/migrate_comments.py before the statuses migration."
                )
            check_integrity(db)

            distinct_statuses = {row[0] for row in db.execute("SELECT DISTINCT status FROM clients")}
            unsupported_statuses = distinct_statuses - CLIENT_STATUSES - set(STATUS_RENAMES)
            if unsupported_statuses:
                names = ", ".join(sorted(repr(value) for value in unsupported_statuses))
                raise ValueError(f"Unsupported client statuses found: {names}. Resolve them explicitly first.")

            has_status = "client_status" in contact_columns
            has_marks = "marks" in contact_columns
            if has_status != has_marks:
                raise ValueError("Partially migrated contact schema found. No changes made.")

            old_contacts = []
            if contact_columns and not has_status:
                old_contacts = list(db.execute(
                    "SELECT id,client_id,result,activity_date,created_at FROM contact_activities "
                    "ORDER BY client_id,activity_date,created_at,id"
                ))
                unknown_results = {
                    row["result"] for row in old_contacts
                    if row["result"] not in RESULT_TO_STATUS and row["result"] not in KNOWN_NEUTRAL_RESULTS
                }
                if unknown_results:
                    names = ", ".join(sorted(repr(value) for value in unknown_results))
                    raise ValueError(f"Unsupported historical contact results found: {names}.")

            if has_status:
                invalid_contact_status = db.execute(
                    "SELECT id,client_status FROM contact_activities "
                    "WHERE client_status NOT IN (?,?,?,?,?,?) LIMIT 1",
                    tuple(sorted(CLIENT_STATUSES)),
                ).fetchone()
                if invalid_contact_status:
                    raise ValueError(
                        f"Contact #{invalid_contact_status['id']} has unsupported status "
                        f"{invalid_contact_status['client_status']!r}."
                    )
                for row in db.execute("SELECT id,marks FROM contact_activities"):
                    parsed_marks(row["marks"], row["id"])

            needs_client_updates = any(status in STATUS_RENAMES for status in distinct_statuses)
            if has_status and not needs_client_updates:
                db.rollback()
                result["already_current"] = True
                return result

            backup_root = backup_dir.resolve() if backup_dir else path.parent / "backups"
            backup_root.mkdir(parents=True, exist_ok=True)
            backup = backup_root / (
                f"{path.stem}_before_statuses_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}.sqlite3"
            )
            create_backup(path, backup)
            result["backup"] = str(backup)
            print(f"Backup saved: {backup}", flush=True)

            for old, new in STATUS_RENAMES.items():
                cursor = db.execute("UPDATE clients SET status=? WHERE status=?", (new, old))
                result["clients_updated"] += cursor.rowcount

            if contact_columns and not has_status:
                db.execute(
                    "ALTER TABLE contact_activities ADD COLUMN client_status "
                    "VARCHAR(64) NOT NULL DEFAULT 'Поиск контакта'"
                )
                db.execute(
                    "ALTER TABLE contact_activities ADD COLUMN marks JSON NOT NULL DEFAULT '[]'"
                )
                state_by_client: dict[int, str] = {}
                for row in old_contacts:
                    state = state_by_client.get(row["client_id"], "Поиск контакта")
                    if row["result"] in RESULT_TO_STATUS:
                        state = RESULT_TO_STATUS[row["result"]]
                    state_by_client[row["client_id"]] = state
                    marks = RESULT_TO_MARKS.get(row["result"], [])
                    db.execute(
                        "UPDATE contact_activities SET client_status=?,marks=? WHERE id=?",
                        (state, json.dumps(marks, ensure_ascii=False, separators=(",", ":")), row["id"]),
                    )
                    result["contacts_updated"] += 1

            if columns(db, "client_imports"):
                cursor = db.execute("UPDATE client_imports SET state='stale' WHERE state='pending'")
                result["previews_staled"] = cursor.rowcount
            db.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO schema_migrations VALUES (?,?)",
                (VERSION, datetime.now(timezone.utc).isoformat()),
            )
            check_integrity(db)
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate TaskManager client statuses and activity marks. Stop the application first."
    )
    parser.add_argument("--database", required=True, type=Path, help="Existing SQLite database path")
    parser.add_argument("--backup-dir", type=Path, help="Default: backups directory beside the database")
    args = parser.parse_args()
    try:
        result = migrate(args.database, args.backup_dir)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Migration failed; database changes rolled back: {exc}", file=sys.stderr)
        return 1
    if result["already_current"]:
        print("Already migrated. No changes made.")
    else:
        print(f"Client statuses updated: {result['clients_updated']}")
        print(f"Contact snapshots updated: {result['contacts_updated']}")
        print(f"Pending import previews invalidated: {result['previews_staled']}")
        print("Integrity and foreign-key checks passed. Migration completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
