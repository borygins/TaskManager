from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from app.core.config import settings


def create_backup() -> Path:
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = settings.backup_dir / f"task_manager_{timestamp}.sqlite3"
    if settings.db_path.exists():
        shutil.copy2(settings.db_path, destination)
    return destination
