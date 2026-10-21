from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from app.services.backup import create_backup


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_dir = TemporaryDirectory()
        self.addCleanup(temporary_dir.cleanup)
        root = Path(temporary_dir.name)
        self.db_path = root / "source.sqlite3"
        self.backup_dir = root / "backups"
        settings_patch = patch(
            "app.services.backup.settings",
            SimpleNamespace(db_path=self.db_path, backup_dir=self.backup_dir),
        )
        settings_patch.start()
        self.addCleanup(settings_patch.stop)

        self.source = sqlite3.connect(self.db_path)
        self.addCleanup(self.source.close)
        self.source.execute("PRAGMA journal_mode=WAL")
        self.source.execute("PRAGMA wal_autocheckpoint=0")
        self.source.execute("CREATE TABLE clients (name TEXT)")
        self.source.commit()
        self.source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.source.execute("INSERT INTO clients VALUES (?)", ("Сохранённый клиент",))
        self.source.commit()

    def assert_backup_contains_committed_client(self, destination: Path) -> None:
        self.assertEqual(destination.parent, self.backup_dir)
        self.assertRegex(destination.name, r"^task_manager_\d{8}_\d{6}\.sqlite3$")
        # Immutable mode reads the standalone backup without consulting WAL/SHM.
        with closing(sqlite3.connect(destination.as_uri() + "?immutable=1", uri=True)) as backup:
            self.assertEqual(
                backup.execute("SELECT name FROM clients").fetchall(),
                [("Сохранённый клиент",)],
            )
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone(), ("ok",))

    def test_backup_includes_committed_data_still_in_wal(self) -> None:
        wal = Path(str(self.db_path) + "-wal")
        self.assertGreater(wal.stat().st_size, 0)
        # Confirm that copying only the main file would miss the committed row.
        with closing(sqlite3.connect(self.db_path.as_uri() + "?immutable=1", uri=True)) as main_file:
            self.assertEqual(main_file.execute("SELECT name FROM clients").fetchall(), [])

        self.assert_backup_contains_committed_client(create_backup())
        self.assertGreater(wal.stat().st_size, 0)

    def test_backup_excludes_uncommitted_changes(self) -> None:
        self.source.execute("INSERT INTO clients VALUES (?)", ("Незавершённое изменение",))
        self.assert_backup_contains_committed_client(create_backup())
        self.assertTrue(self.source.in_transaction)
        self.source.rollback()


if __name__ == "__main__":
    unittest.main()
