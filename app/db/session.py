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
