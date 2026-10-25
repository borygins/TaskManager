"""Offline SQLite migration. Does not import or start the application.

Run explicitly, with the application stopped:
    python scripts/migrate_comments.py --database task_manager.sqlite3
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import time
from uuid import uuid4


VERSION = "20261025_unified_comments_auto_funnel"
OLD_COMMENT_COLUMNS = {"id", "client_id", "author_id", "body", "created_at"}
OLD_CONTACT_COLUMNS = {
    "id", "request_key", "client_id", "manager_id", "funnel_id", "activity_date",
    "activity_type", "dialogue", "result", "potential_turnover", "next_activity_date",
    "comment", "tax_number", "company_name", "funnel_name", "created_at",
}
UNIFIED_COMMENT_COLUMNS = OLD_COMMENT_COLUMNS | {"source"}
UNIFIED_CONTACT_COLUMNS = (OLD_CONTACT_COLUMNS - {"comment"}) | {"comment_id"}


def columns(connection: sqlite3.Connection, table: str) -> dict:
    # Table names are internal constants, never user input.
    return {row[1]: row for row in connection.execute(f'PRAGMA table_info("{table}")')}


def check_integrity(connection: sqlite3.Connection) -> None:
    if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
        raise ValueError("SQLite integrity_check failed. Restore/repair the database before migration.")
    if connection.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("Foreign-key violations found. Migration stopped without deleting data.")


def verify_links(connection: sqlite3.Connection) -> None:
    if not columns(connection, "contact_activities"):
        return
    invalid = connection.execute("""
        SELECT a.id FROM contact_activities a LEFT JOIN comments c ON c.id = a.comment_id
        WHERE c.id IS NULL OR a.client_id != c.client_id
           OR c.author_id IS NOT a.manager_id OR c.source != 'contact'
        LIMIT 1
    """).fetchone()
    if invalid:
        raise ValueError(f"Invalid comment link for contact #{invalid[0]}.")
    if connection.execute("""
        SELECT comment_id FROM contact_activities GROUP BY comment_id HAVING COUNT(*) > 1 LIMIT 1
    """).fetchone():
        raise ValueError("Several contacts reference the same comment.")


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def create_backup(path: Path, destination: Path) -> None:
    """The writer reservation is held by another connection; no changes yet."""
    deadline = time.monotonic() + 60

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError("Backup took more than 60 seconds. Migration was not started.")

    # Never overwrite an existing backup, even when two invocations race.
    with destination.open("xb"):
        pass
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target, pages=256, progress=progress)
            check_integrity(target)


def migrate(database: Path, backup_dir: Path | None = None) -> dict:
    path = database.resolve(strict=True)
    if not path.is_file():
        raise ValueError("Database must be an existing file.")
    if sqlite3.sqlite_version_info < (3, 35, 0):
        raise ValueError("SQLite 3.35+ is required to remove the old notes column.")
    result = {"already_current": False, "backup": None, "linked": 0,
              "created_for_contacts": 0, "ambiguous": 0, "notes_added": 0,
              "notes_already_present": 0, "contact_schema_upgraded": False}
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        # Required for SQLite's transactional table-rebuild procedure. Must be set
        # before BEGIN; foreign_key_check is mandatory before COMMIT.
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN IMMEDIATE")
        try:
            clients = columns(db, "clients")
            comments = columns(db, "comments")
            contacts = columns(db, "contact_activities")
            if not clients or not comments or not columns(db, "users"):
                raise ValueError("Not a supported TaskManager database (clients/comments/users required).")
            check_integrity(db)
            unified = ("manager_notes" not in clients and set(comments) == UNIFIED_COMMENT_COLUMNS
                       and not comments["author_id"][3]
                       and (not contacts or set(contacts) == UNIFIED_CONTACT_COLUMNS))
            current = unified and (not contacts or not contacts["funnel_id"][3])
            if current:
                verify_links(db)
                db.rollback()
                result["already_current"] = True
                return result
            # Databases migrated by the first version of this script only need
            # funnel_id changed from NOT NULL to nullable. This preserves every
            # row and makes the new “Прочее” fallback possible.
            if unified and contacts and contacts["funnel_id"][3]:
                if db.execute("""SELECT 1 FROM sqlite_master WHERE
                    (type='trigger' AND tbl_name='contact_activities') OR type='view' LIMIT 1""").fetchone():
                    raise ValueError("Custom triggers/views found. Review them before this migration.")
                if columns(db, "contact_activities__unified"):
                    raise ValueError("Unexpected staging table: contact_activities__unified.")
                backup_root = backup_dir.resolve() if backup_dir else path.parent / "backups"
                backup_root.mkdir(parents=True, exist_ok=True)
                backup = backup_root / f"{path.stem}_before_comments_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}.sqlite3"
                create_backup(path, backup)
                result["backup"] = str(backup)
                print(f"Backup saved: {backup}", flush=True)
                old_contacts = list(db.execute("SELECT * FROM contact_activities ORDER BY id"))
                indexes = [row[0] for row in db.execute("""SELECT sql FROM sqlite_master
                    WHERE type='index' AND tbl_name='contact_activities' AND sql IS NOT NULL""")]
                db.execute("""CREATE TABLE contact_activities__unified (
                    id INTEGER NOT NULL PRIMARY KEY, request_key VARCHAR(64) NOT NULL UNIQUE,
                    client_id INTEGER NOT NULL REFERENCES clients(id),
                    manager_id INTEGER NOT NULL REFERENCES users(id),
                    funnel_id INTEGER REFERENCES funnels(id), activity_date DATE NOT NULL,
                    activity_type VARCHAR(32) NOT NULL, dialogue BOOLEAN NOT NULL,
                    result VARCHAR(64) NOT NULL, potential_turnover NUMERIC(14,2) NOT NULL,
                    next_activity_date DATE, comment_id INTEGER NOT NULL UNIQUE REFERENCES comments(id),
                    tax_number VARCHAR(64) NOT NULL, company_name VARCHAR(255) NOT NULL,
                    funnel_name VARCHAR(255) NOT NULL, created_at DATETIME NOT NULL
                )""")
                fields = list(contacts)
                field_sql = ",".join('"' + field + '"' for field in fields)
                placeholders = ",".join("?" for _ in fields)
                db.executemany(f"INSERT INTO contact_activities__unified ({field_sql}) VALUES ({placeholders})",
                               [tuple(row[field] for field in fields) for row in old_contacts])
                for row in old_contacts:
                    saved = db.execute("SELECT * FROM contact_activities__unified WHERE id=?", (row["id"],)).fetchone()
                    if any(saved[field] != row[field] for field in fields):
                        raise ValueError(f"Contact #{row['id']} did not round-trip correctly.")
                db.execute("DROP TABLE contact_activities")
                db.execute("ALTER TABLE contact_activities__unified RENAME TO contact_activities")
                for sql in indexes:
                    db.execute(sql)
                verify_links(db)
                check_integrity(db)
                db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
                db.execute("INSERT OR IGNORE INTO schema_migrations VALUES (?,?)",
                           (VERSION, datetime.now(timezone.utc).isoformat()))
                db.commit()
                result["contact_schema_upgraded"] = True
                return result
            if set(comments) != OLD_COMMENT_COLUMNS or (contacts and set(contacts) != OLD_CONTACT_COLUMNS):
                raise ValueError("Unknown or partially migrated schema. No changes made; inspect it manually.")
            # Rebuild only known table layouts; preserve custom indexes, but do not
            # silently lose custom triggers or views that depend on the old fields.
            if db.execute("""SELECT 1 FROM sqlite_master WHERE
                (type='trigger' AND tbl_name IN ('comments','contact_activities','clients'))
                OR type='view' LIMIT 1""").fetchone():
                raise ValueError("Custom triggers/views found. Review them before this migration.")
            for name in ("comments__unified", "contact_activities__unified"):
                if columns(db, name):
                    raise ValueError(f"Unexpected staging table: {name}.")
            if "schema_migrations" in {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
                if db.execute("SELECT 1 FROM schema_migrations WHERE version=?", (VERSION,)).fetchone():
                    raise ValueError("Migration marker exists but the schema is still old.")

            backup_root = backup_dir.resolve() if backup_dir else path.parent / "backups"
            backup_root.mkdir(parents=True, exist_ok=True)
            backup = backup_root / f"{path.stem}_before_comments_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}.sqlite3"
            create_backup(path, backup)
            result["backup"] = str(backup)
            print(f"Backup saved: {backup}", flush=True)

            old_comments = list(db.execute("SELECT * FROM comments ORDER BY id"))
            old_contacts = list(db.execute("SELECT * FROM contact_activities ORDER BY id")) if contacts else []
            indexes = [row[0] for row in db.execute("""SELECT sql FROM sqlite_master
                WHERE type='index' AND tbl_name IN ('comments','contact_activities') AND sql IS NOT NULL""")]
            db.execute("""CREATE TABLE comments__unified (
                id INTEGER NOT NULL PRIMARY KEY,
                client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                author_id INTEGER REFERENCES users(id), body TEXT NOT NULL,
                source VARCHAR(32) NOT NULL, created_at DATETIME NOT NULL
            )""")
            db.executemany("""INSERT INTO comments__unified
                (id,client_id,author_id,body,source,created_at) VALUES (?,?,?,?,'legacy',?)""",
                [(r["id"], r["client_id"], r["author_id"], r["body"], r["created_at"]) for r in old_comments])
            for row in old_comments:
                saved = db.execute("SELECT * FROM comments__unified WHERE id=?", (row["id"],)).fetchone()
                if any(saved[field] != row[field] for field in OLD_COMMENT_COLUMNS):
                    raise ValueError(f"Comment #{row['id']} did not round-trip correctly.")
            for row in old_comments:
                saved = db.execute("SELECT * FROM comments__unified WHERE id=?", (row["id"],)).fetchone()
                if any(saved[field] != row[field] for field in OLD_COMMENT_COLUMNS):
                    raise ValueError(f"Comment #{row['id']} did not round-trip correctly.")

            # Matching is one-to-one in BOTH directions. Identical text alone is
            # insufficient: repeated calls must remain distinct events.
            by_content = defaultdict(list)
            for row in old_comments:
                by_content[(row["client_id"], row["author_id"], row["body"])].append(row)
            candidates = {}
            for contact in old_contacts:
                matches = []
                for row in by_content[(contact["client_id"], contact["manager_id"], contact["comment"])]:
                    try:
                        delay = (timestamp(row["created_at"]) - timestamp(contact["created_at"])).total_seconds()
                    except (ValueError, TypeError):
                        continue
                    if 0 <= delay <= 5:
                        matches.append(row["id"])
                candidates[contact["id"]] = matches
            usages = Counter(identifier for matches in candidates.values() for identifier in matches)
            links = {}
            for contact in old_contacts:
                matches = candidates[contact["id"]]
                if len(matches) == 1 and usages[matches[0]] == 1:
                    identifier = matches[0]
                    db.execute("UPDATE comments__unified SET source='contact' WHERE id=?", (identifier,))
                    result["linked"] += 1
                else:
                    if matches:
                        result["ambiguous"] += 1
                    identifier = db.execute("""INSERT INTO comments__unified
                        (client_id,author_id,body,source,created_at) VALUES (?,?,?,'contact',?)""",
                        (contact["client_id"], contact["manager_id"], contact["comment"], contact["created_at"])).lastrowid
                    result["created_for_contacts"] += 1
                links[contact["id"]] = identifier

            if "manager_notes" in clients:
                known = defaultdict(set)
                for row in db.execute("SELECT client_id,body FROM comments__unified"):
                    known[row["client_id"]].add(" ".join(row["body"].split()))
                now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
                for row in db.execute("SELECT id,manager_notes FROM clients"):
                    body = row["manager_notes"] or ""
                    normalized = " ".join(body.split())
                    if not normalized:
                        continue
                    if normalized in known[row["id"]]:
                        result["notes_already_present"] += 1
                        continue
                    db.execute("""INSERT INTO comments__unified
                        (client_id,author_id,body,source,created_at) VALUES (?,NULL,?,'legacy_notes',?)""",
                        (row["id"], body, now))
                    known[row["id"]].add(normalized)
                    result["notes_added"] += 1

            if contacts:
                db.execute("""CREATE TABLE contact_activities__unified (
                    id INTEGER NOT NULL PRIMARY KEY, request_key VARCHAR(64) NOT NULL UNIQUE,
                    client_id INTEGER NOT NULL REFERENCES clients(id),
                    manager_id INTEGER NOT NULL REFERENCES users(id),
                    funnel_id INTEGER REFERENCES funnels(id), activity_date DATE NOT NULL,
                    activity_type VARCHAR(32) NOT NULL, dialogue BOOLEAN NOT NULL,
                    result VARCHAR(64) NOT NULL, potential_turnover NUMERIC(14,2) NOT NULL,
                    next_activity_date DATE, comment_id INTEGER NOT NULL UNIQUE REFERENCES comments(id),
                    tax_number VARCHAR(64) NOT NULL, company_name VARCHAR(255) NOT NULL,
                    funnel_name VARCHAR(255) NOT NULL, created_at DATETIME NOT NULL
                )""")
                fields = [name for name in contacts if name != "comment"]
                field_sql = ",".join('"' + field + '"' for field in fields)
                placeholders = ",".join("?" for _ in range(len(fields) + 1))
                db.executemany(f"INSERT INTO contact_activities__unified ({field_sql},comment_id) VALUES ({placeholders})",
                               [tuple(row[field] for field in fields) + (links[row["id"]],) for row in old_contacts])
                # Validate unchanged contact snapshots and comment text before
                # removing either legacy table.
                for row in old_contacts:
                    saved = db.execute("SELECT * FROM contact_activities__unified WHERE id=?", (row["id"],)).fetchone()
                    body = db.execute("SELECT body FROM comments__unified WHERE id=?", (saved["comment_id"],)).fetchone()[0]
                    if any(saved[field] != row[field] for field in fields) or body != row["comment"]:
                        raise ValueError(f"Contact #{row['id']} did not round-trip correctly.")
                db.execute("DROP TABLE contact_activities")
            db.execute("DROP TABLE comments")
            db.execute("ALTER TABLE comments__unified RENAME TO comments")
            if contacts:
                db.execute("ALTER TABLE contact_activities__unified RENAME TO contact_activities")
            for sql in indexes:
                db.execute(sql)
            if "manager_notes" in clients:
                db.execute("ALTER TABLE clients DROP COLUMN manager_notes")
            # Reject stale import previews: their saved plan used the old model.
            if columns(db, "client_imports"):
                db.execute("UPDATE client_imports SET state='stale' WHERE state='pending'")
            verify_links(db)
            check_integrity(db)
            db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
            db.execute("INSERT INTO schema_migrations VALUES (?,?)", (VERSION, datetime.now(timezone.utc).isoformat()))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys=ON")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Unify TaskManager comments. Stop the application first. A backup is mandatory and automatic.")
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
        if result["contact_schema_upgraded"]:
            print("Contact funnel field upgraded for automatic 'Прочее' values.")
        print(f"Contacts linked: {result['linked']}")
        print(f"Comments created for contacts: {result['created_for_contacts']}")
        print(f"Ambiguous contacts (old comments preserved): {result['ambiguous']}")
        print(f"Notes transferred: {result['notes_added']}; already present: {result['notes_already_present']}")
        print("Integrity and foreign-key checks passed. Migration completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
