from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from app.core.config import settings


def create_backup() -> Path:
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = settings.backup_dir / f"task_manager_{timestamp}.sqlite3"
    if settings.db_path.exists():
        source_uri = settings.db_path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True)) as source:
            with closing(sqlite3.connect(destination)) as backup:
                source.backup(backup)
    return destination
