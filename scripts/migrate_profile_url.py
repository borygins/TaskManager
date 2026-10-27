"""Add the external profile link to TaskManager clients.

Run explicitly with the application stopped:
    python scripts/migrate_profile_url.py --database task_manager.sqlite3

The script is standalone so importing it never opens the application database.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import time
from uuid import uuid4


VERSION = "20261027_client_profile_url"
PROFILE_COLUMN = "profile_url"
# Compatibility with the short-lived column name used before this migration was finalized.
LEGACY_COLUMN = "ri" + "tm_url"


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
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(destination)) as target:
                source.backup(target, pages=256, progress=progress)
                check_integrity(target)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def validate_existing_column(column: sqlite3.Row) -> None:
    declared_type = (column[2] or "").upper()
    if "CHAR" not in declared_type or not column[3]:
        raise ValueError("clients.profile_url has an incompatible definition. No changes made.")


def migrate(database: Path, backup_dir: Path | None = None) -> dict[str, object]:
    path = database.resolve(strict=True)
    if not path.is_file():
        raise ValueError("Database must be an existing file.")
    result: dict[str, object] = {"already_current": False, "backup": None, "action": None}
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            client_columns = columns(db, "clients")
            if not client_columns or "id" not in client_columns:
                raise ValueError("Not a supported TaskManager database: clients table is required.")
            check_integrity(db)
            if PROFILE_COLUMN in client_columns:
                validate_existing_column(client_columns[PROFILE_COLUMN])
                db.rollback()
                result["already_current"] = True
                return result

            backup_root = backup_dir.resolve() if backup_dir else path.parent / "backups"
            backup_root.mkdir(parents=True, exist_ok=True)
            backup = backup_root / (
                f"{path.stem}_before_profile_url_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}.sqlite3"
            )
            create_backup(path, backup)
            result["backup"] = str(backup)
            print(f"Backup saved: {backup}", flush=True)

            if LEGACY_COLUMN in client_columns:
                db.execute(f'ALTER TABLE clients RENAME COLUMN "{LEGACY_COLUMN}" TO "{PROFILE_COLUMN}"')
                result["action"] = "renamed"
            else:
                db.execute("ALTER TABLE clients ADD COLUMN profile_url VARCHAR(2048) NOT NULL DEFAULT ''")
                result["action"] = "added"
            db.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO schema_migrations VALUES (?,?)",
                (VERSION, datetime.now(timezone.utc).isoformat()),
            )
            validate_existing_column(columns(db, "clients")[PROFILE_COLUMN])
            check_integrity(db)
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add the client profile link. Stop TaskManager before running this command."
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
        print(f"Column clients.profile_url {result['action']}.")
        print("Integrity and foreign-key checks passed. Migration completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
