from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = BASE_DIR / ".env"


def _load_env_file(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue

        os.environ[key] = value.strip().strip("'\"")


_load_env_file()


@dataclass(slots=True)
class Settings:
    app_name: str = "Sales Task Manager"
    secret_key: str = "change-me-in-production"
    db_path: Path = BASE_DIR / "task_manager.sqlite3"
    backup_dir: Path = BASE_DIR / "backups"
    upload_dir: Path = BASE_DIR / "app" / "static" / "uploads"
    templates_dir: Path = BASE_DIR / "app" / "templates"
    static_dir: Path = BASE_DIR / "app" / "static"
    default_statuses: tuple[str, ...] = (
        "Поиск контакта",
        "Отказ",
        "Переговоры",
        "Согласие",
        "Мискодинг",
        "Подписан",
    )
    funnel_names: tuple[str, ...] = ("Доля рынка", "Спарк")
    backup_hour: int = 2
    host: str = "0.0.0.0"
    port: int = 8000
    seeded_users: dict[str, dict[str, str]] = field(
        default_factory=lambda: {
            "01333777": {
                "password": os.getenv("SEEDED_BOSS_PASSWORD", ""),
                "full_name": "Тестовый начальник",
                "role": "boss",
            },
            "01666222": {
                "password": os.getenv("SEEDED_MANAGER_PASSWORD", ""),
                "full_name": "Тестовый менеджер",
                "role": "manager",
            },
        }
    )

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path.as_posix()}"


settings = Settings()
