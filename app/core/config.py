from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]


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
                "password": "admin",
                "full_name": "Тестовый начальник",
                "role": "boss",
            },
            "01666222": {
                "password": "manager",
                "full_name": "Тестовый менеджер",
                "role": "manager",
            },
        }
    )

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path.as_posix()}"


settings = Settings()
