from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.migrate_comments import VERSION, columns, migrate


class CommentsMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "legacy.sqlite3"
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE users (id INTEGER PRIMARY KEY);
                CREATE TABLE funnels (id INTEGER PRIMARY KEY);
                INSERT INTO users VALUES (1);
                INSERT INTO funnels VALUES (1);
                CREATE TABLE clients (id INTEGER PRIMARY KEY, manager_notes TEXT NOT NULL);
                INSERT INTO clients VALUES (1, 'Старая заметка'), (2, 'Уже есть');
                CREATE TABLE comments (
                    id INTEGER NOT NULL PRIMARY KEY,
                    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                    author_id INTEGER NOT NULL REFERENCES users(id),
                    body TEXT NOT NULL, created_at DATETIME NOT NULL
                );
                CREATE INDEX ix_comments_client_id ON comments(client_id);
                CREATE INDEX ix_comments_author_id ON comments(author_id);
                CREATE INDEX ix_comments_created_at ON comments(created_at);
                INSERT INTO comments VALUES (1, 1, 1, 'Звонок', '2026-10-20 12:00:00.100000');
                INSERT INTO comments VALUES (2, 2, 1, 'Уже  есть', '2026-10-19 12:00:00');
                CREATE TABLE contact_activities (
                    id INTEGER NOT NULL PRIMARY KEY, request_key VARCHAR(64) NOT NULL UNIQUE,
                    client_id INTEGER NOT NULL REFERENCES clients(id),
                    manager_id INTEGER NOT NULL REFERENCES users(id),
                    funnel_id INTEGER NOT NULL REFERENCES funnels(id), activity_date DATE NOT NULL,
                    activity_type VARCHAR(32) NOT NULL, dialogue BOOLEAN NOT NULL,
                    result VARCHAR(64) NOT NULL, potential_turnover NUMERIC(14,2) NOT NULL,
                    next_activity_date DATE, comment TEXT NOT NULL, tax_number VARCHAR(64) NOT NULL,
                    company_name VARCHAR(255) NOT NULL, funnel_name VARCHAR(255) NOT NULL,
                    created_at DATETIME NOT NULL
                );
                CREATE INDEX ix_contact_activities_client_id ON contact_activities(client_id);
                CREATE INDEX ix_contact_activities_manager_id ON contact_activities(manager_id);
                CREATE INDEX ix_contact_activities_activity_date ON contact_activities(activity_date);
                CREATE TABLE client_imports (id TEXT PRIMARY KEY, state TEXT NOT NULL);
                INSERT INTO client_imports VALUES ('pending', 'pending'), ('applied', 'applied');
            """)
            self.add_contact(db, 1, "Звонок", "2026-10-20 12:00:00")
            self.add_contact(db, 2, "Нет копии", "2026-10-20 13:00:00")
            db.commit()

    def add_contact(self, db, identifier, body, created_at):
        db.execute("INSERT INTO contact_activities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            identifier, f"request-{identifier}", 1, 1, 1, "2026-10-20", "Звонок", 1,
            "Переговоры", 123.45, "2026-10-28", body, "001234567890", "Организация", "Воронка", created_at,
        ))

    def test_preserves_data_backs_up_and_is_idempotent(self):
        result = migrate(self.path)
        self.assertEqual(result["linked"], 1)
        self.assertEqual(result["created_for_contacts"], 1)
        self.assertEqual(result["notes_added"], 1)
        self.assertEqual(result["notes_already_present"], 1)
        with closing(sqlite3.connect(result["backup"])) as backup:
            self.assertIn("manager_notes", columns(backup, "clients"))
            self.assertIn("comment", columns(backup, "contact_activities"))
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 2)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertNotIn("manager_notes", columns(db, "clients"))
            self.assertNotIn("comment", columns(db, "contact_activities"))
            self.assertFalse(columns(db, "comments")["author_id"][3])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT body,author_id,source FROM comments WHERE source='legacy_notes'").fetchone(),
                             ("Старая заметка", None, "legacy_notes"))
            self.assertEqual(db.execute("SELECT source,created_at FROM comments WHERE id=1").fetchone(),
                             ("contact", "2026-10-20 12:00:00.100000"))
            self.assertEqual(db.execute("SELECT comment_id FROM contact_activities WHERE id=1").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT c.body FROM contact_activities a JOIN comments c ON c.id=a.comment_id WHERE a.id=2").fetchone()[0], "Нет копии")
            self.assertEqual(db.execute("SELECT state FROM client_imports WHERE id='pending'").fetchone()[0], "stale")
            self.assertEqual(db.execute("SELECT state FROM client_imports WHERE id='applied'").fetchone()[0], "applied")
            self.assertEqual(db.execute("SELECT version FROM schema_migrations").fetchone()[0], VERSION)
            self.assertEqual(list(db.execute("PRAGMA foreign_key_check")), [])
            db.execute("PRAGMA foreign_keys=ON")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM comments WHERE id=1")
            db.rollback()
        second = migrate(self.path)
        self.assertTrue(second["already_current"])
        self.assertEqual(len(list((self.root / "backups").glob("*.sqlite3"))), 1)

    def test_ambiguous_matches_never_merge_calls_or_delete_comments(self):
        with closing(sqlite3.connect(self.path)) as db:
            self.add_contact(db, 3, "Звонок", "2026-10-20 12:00:00")
            db.commit()
        result = migrate(self.path)
        self.assertEqual(result["ambiguous"], 2)
        with closing(sqlite3.connect(self.path)) as db:
            identifiers = [r[0] for r in db.execute("SELECT comment_id FROM contact_activities WHERE id IN (1,3)")]
            self.assertEqual(len(set(identifiers)), 2)
            self.assertNotIn(1, identifiers)
            self.assertEqual(db.execute("SELECT body,source FROM comments WHERE id=1").fetchone(), ("Звонок", "legacy"))

    def test_multiple_candidate_comments_are_preserved(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("INSERT INTO comments VALUES (3,1,1,'Звонок','2026-10-20 12:00:00.200000')")
            db.commit()
        result = migrate(self.path)
        self.assertEqual(result["ambiguous"], 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM comments WHERE id IN (1,3)").fetchone()[0], 2)
            self.assertNotIn(db.execute("SELECT comment_id FROM contact_activities WHERE id=1").fetchone()[0], (1, 3))

    def test_failure_after_rebuild_rolls_back_everything(self):
        with patch("scripts.migrate_comments.verify_links", side_effect=ValueError("Simulated validation error")):
            with self.assertRaisesRegex(ValueError, "Simulated"):
                migrate(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertIn("manager_notes", columns(db, "clients"))
            self.assertIn("comment", columns(db, "contact_activities"))
            self.assertNotIn("source", columns(db, "comments"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 2)
            self.assertFalse(columns(db, "comments__unified"))
            self.assertEqual(db.execute("SELECT state FROM client_imports WHERE id='pending'").fetchone()[0], "pending")
        self.assertEqual(len(list((self.root / "backups").glob("*.sqlite3"))), 1)

    def test_unknown_schema_and_missing_file_are_not_changed(self):
        missing = self.root / "missing.sqlite3"
        with self.assertRaises(FileNotFoundError):
            migrate(missing)
        self.assertFalse(missing.exists())
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("ALTER TABLE comments ADD COLUMN custom_data TEXT")
            db.commit()
        with self.assertRaisesRegex(ValueError, "Unknown"):
            migrate(self.path)
        self.assertFalse((self.root / "backups").exists())

    def test_older_database_without_contacts_is_supported(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP TABLE contact_activities")
            db.commit()
        result = migrate(self.path)
        self.assertEqual(result["notes_added"], 1)
        self.assertEqual(result["linked"], 0)
        self.assertTrue(migrate(self.path)["already_current"])

    def test_migrated_tables_can_be_read_by_new_orm(self):
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import Session, joinedload
        from app.models import Comment, ContactActivity

        migrate(self.path)
        engine = create_engine(f"sqlite:///{self.path.as_posix()}")
        try:
            with Session(engine) as session:
                contacts = list(session.scalars(select(ContactActivity).options(
                    joinedload(ContactActivity.comment_record)).order_by(ContactActivity.id)))
                self.assertEqual([contact.comment_record.body for contact in contacts], ["Звонок", "Нет копии"])
                self.assertEqual(contacts[0].comment_record.source, "contact")
                note = session.scalar(select(Comment).where(Comment.source == "legacy_notes"))
                self.assertIsNone(note.author_id)
                self.assertEqual(note.body, "Старая заметка")
        finally:
            engine.dispose()

    def test_first_migration_version_is_upgraded_for_other_channel(self):
        migrate(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.executescript("""
                ALTER TABLE contact_activities RENAME TO contact_activities_nullable;
                CREATE TABLE contact_activities (
                    id INTEGER NOT NULL PRIMARY KEY, request_key VARCHAR(64) NOT NULL UNIQUE,
                    client_id INTEGER NOT NULL REFERENCES clients(id),
                    manager_id INTEGER NOT NULL REFERENCES users(id),
                    funnel_id INTEGER NOT NULL REFERENCES funnels(id), activity_date DATE NOT NULL,
                    activity_type VARCHAR(32) NOT NULL, dialogue BOOLEAN NOT NULL,
                    result VARCHAR(64) NOT NULL, potential_turnover NUMERIC(14,2) NOT NULL,
                    next_activity_date DATE, comment_id INTEGER NOT NULL UNIQUE REFERENCES comments(id),
                    tax_number VARCHAR(64) NOT NULL, company_name VARCHAR(255) NOT NULL,
                    funnel_name VARCHAR(255) NOT NULL, created_at DATETIME NOT NULL
                );
                INSERT INTO contact_activities SELECT * FROM contact_activities_nullable;
                DROP TABLE contact_activities_nullable;
            """)
            db.commit()
        result = migrate(self.path)
        self.assertTrue(result["contact_schema_upgraded"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertFalse(columns(db, "contact_activities")["funnel_id"][3])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM contact_activities").fetchone()[0], 2)
            self.assertEqual(list(db.execute("PRAGMA foreign_key_check")), [])


if __name__ == "__main__":
    unittest.main()
