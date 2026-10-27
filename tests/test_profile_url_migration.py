from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from scripts.migrate_profile_url import LEGACY_COLUMN, VERSION, columns, migrate


class ProfileUrlMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "legacy.sqlite3"
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE clients (
                    id INTEGER PRIMARY KEY,
                    full_name VARCHAR(255) NOT NULL,
                    company_name VARCHAR(255) NOT NULL
                );
                INSERT INTO clients VALUES (1, 'Иван Иванов', 'ООО Ромашка');
            """)

    def test_adds_column_backs_up_and_is_idempotent(self):
        result = migrate(self.path)
        self.assertFalse(result["already_current"])
        self.assertEqual(result["action"], "added")
        backup = Path(str(result["backup"]))
        self.assertTrue(backup.is_file())
        with closing(sqlite3.connect(backup)) as db:
            self.assertNotIn("profile_url", columns(db, "clients"))
            self.assertEqual(db.execute("SELECT full_name FROM clients").fetchone()[0], "Иван Иванов")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertIn("profile_url", columns(db, "clients"))
            self.assertEqual(db.execute("SELECT profile_url FROM clients WHERE id=1").fetchone()[0], "")
            self.assertEqual(
                db.execute("SELECT version FROM schema_migrations WHERE version=?", (VERSION,)).fetchone()[0],
                VERSION,
            )

        repeated = migrate(self.path)
        self.assertTrue(repeated["already_current"])
        self.assertIsNone(repeated["backup"])

    def test_renames_transitional_column_and_preserves_data(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(f'ALTER TABLE clients ADD COLUMN "{LEGACY_COLUMN}" VARCHAR(2048) NOT NULL DEFAULT \'\'')
            db.execute(f'UPDATE clients SET "{LEGACY_COLUMN}"=? WHERE id=1', ("https://profile.example/1",))
            db.commit()

        result = migrate(self.path)
        self.assertEqual(result["action"], "renamed")
        with closing(sqlite3.connect(self.path)) as db:
            client_columns = columns(db, "clients")
            self.assertIn("profile_url", client_columns)
            self.assertNotIn(LEGACY_COLUMN, client_columns)
            self.assertEqual(db.execute("SELECT profile_url FROM clients WHERE id=1").fetchone()[0], "https://profile.example/1")

    def test_rejects_database_without_clients_table(self):
        empty_path = self.root / "empty.sqlite3"
        with closing(sqlite3.connect(empty_path)):
            pass
        with self.assertRaisesRegex(ValueError, "clients table is required"):
            migrate(empty_path)


if __name__ == "__main__":
    unittest.main()
