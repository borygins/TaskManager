from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    pass


engine: AsyncEngine = create_async_engine(settings.database_url, echo=False, future=True)


@event.listens_for(engine.sync_engine, "connect")
def configure_sqlite(dbapi_connection, connection_record) -> None:
    _ = connection_record
    dbapi_connection.create_function(
        "casefold",
        1,
        lambda value: value.casefold() if value is not None else None,
        deterministic=True,
    )
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.close()


SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    from app.models import entities  # noqa: F401

    async with engine.begin() as connection:
        # create_all cannot migrate existing columns. Never silently start the new
        # model on an old database or run the destructive migration at startup.
        old_clients = {row[1] for row in (await connection.execute(text("PRAGMA table_info(clients)"))).fetchall()}
        old_comments = (await connection.execute(text("PRAGMA table_info(comments)"))).fetchall()
        old_contacts = {row[1] for row in (await connection.execute(text("PRAGMA table_info(contact_activities)"))).fetchall()}
        if ("manager_notes" in old_clients
                or (old_comments and ("source" not in {row[1] for row in old_comments}
                                      or any(row[1] == "author_id" and row[3] for row in old_comments)))
                or (old_contacts and ("comment" in old_contacts or "comment_id" not in old_contacts))):
            raise RuntimeError(
                "Требуется миграция комментариев. Остановите приложение и выполните "
                "python scripts/migrate_comments.py --database task_manager.sqlite3 "
                "(или укажите путь к своей базе). Инструкция: docs/COMMENTS_MIGRATION.md"
            )
        await connection.run_sync(Base.metadata.create_all)

        client_columns = {row[1] for row in (await connection.execute(text("PRAGMA table_info(clients)"))).fetchall()}
        if "last_manager_activity_date" not in client_columns:
            await connection.execute(text("ALTER TABLE clients ADD COLUMN last_manager_activity_date DATE"))

        user_columns = {row[1] for row in (await connection.execute(text("PRAGMA table_info(users)"))).fetchall()}
        if "role_expires_at" not in user_columns:
            await connection.execute(text("ALTER TABLE users ADD COLUMN role_expires_at DATE"))
        if "role_after_expiry" not in user_columns:
            await connection.execute(text("ALTER TABLE users ADD COLUMN role_after_expiry VARCHAR(32)"))
        if "must_change_password" not in user_columns:
            await connection.execute(text("ALTER TABLE users ADD COLUMN must_change_password BOOLEAN DEFAULT 0"))

        await connection.execute(text("PRAGMA wal_checkpoint(TRUNCATE);"))
