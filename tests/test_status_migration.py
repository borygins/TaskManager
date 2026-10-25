from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from scripts.migrate_statuses import migrate


class StatusMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "task_manager.sqlite3"
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE users (id INTEGER PRIMARY KEY);
                CREATE TABLE funnels (id INTEGER PRIMARY KEY);
                CREATE TABLE comments (id INTEGER PRIMARY KEY);
                CREATE TABLE clients (
                    id INTEGER PRIMARY KEY,
                    status VARCHAR(64) NOT NULL
                );
                CREATE TABLE contact_activities (
                    id INTEGER PRIMARY KEY,
                    client_id INTEGER NOT NULL REFERENCES clients(id),
                    funnel_id INTEGER REFERENCES funnels(id),
                    comment_id INTEGER NOT NULL REFERENCES comments(id),
                    result VARCHAR(64) NOT NULL,
                    activity_date DATE NOT NULL,
                    created_at DATETIME NOT NULL
                );
                CREATE TABLE client_imports (id TEXT PRIMARY KEY, state TEXT NOT NULL);
                INSERT INTO users VALUES (1);
                INSERT INTO clients VALUES
                    (1,'Согласие'),
                    (2,'Переговоры');
                INSERT INTO comments VALUES (1),(2),(3),(4);
                INSERT INTO contact_activities VALUES
                    (1,1,NULL,1,'Недозвон','2026-10-20','2026-10-20 10:00:00'),
                    (2,1,NULL,2,'Согласие','2026-10-21','2026-10-21 10:00:00'),
                    (3,1,NULL,3,'Перенос на дату','2026-10-22','2026-10-22 10:00:00'),
                    (4,2,NULL,4,'Договор запущен','2026-10-20','2026-10-20 11:00:00');
                INSERT INTO client_imports VALUES ('pending','pending'),('done','applied');
            """)
            db.commit()

    def test_migrates_statuses_snapshots_marks_and_is_idempotent(self):
        result = migrate(self.path)
        self.assertEqual(result["clients_updated"], 1)
        self.assertEqual(result["contacts_updated"], 4)
        self.assertEqual(result["previews_staled"], 1)
        self.assertTrue(Path(result["backup"]).is_file())

        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status FROM clients WHERE id=1").fetchone()[0], "Переговоры")
            rows = list(db.execute(
                "SELECT client_status,marks FROM contact_activities ORDER BY id"
            ))
            self.assertEqual([row[0] for row in rows], [
                "Поиск контакта", "Переговоры", "Переговоры", "Подписан",
            ])
            self.assertEqual([json.loads(row[1]) for row in rows], [
                ["missed_call"], [], ["call_back"], [],
            ])
            self.assertEqual(db.execute(
                "SELECT state FROM client_imports WHERE id='pending'"
            ).fetchone()[0], "stale")
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

        second = migrate(self.path)
        self.assertTrue(second["already_current"])
        self.assertIsNone(second["backup"])

    def test_unknown_status_or_result_is_rejected_without_backup(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE clients SET status='Неизвестно' WHERE id=1")
            db.commit()
        with self.assertRaisesRegex(ValueError, "Unsupported client statuses"):
            migrate(self.path)
        self.assertFalse((self.root / "backups").exists())

        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE clients SET status='Переговоры' WHERE id=1")
            db.execute("UPDATE contact_activities SET result='Неизвестно' WHERE id=1")
            db.commit()
        with self.assertRaisesRegex(ValueError, "Unsupported historical contact results"):
            migrate(self.path)
        self.assertFalse((self.root / "backups").exists())


if __name__ == "__main__":
    unittest.main()
