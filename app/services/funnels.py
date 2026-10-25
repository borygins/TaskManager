from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Funnel


class FunnelAlreadyExists(ValueError):
    pass


async def create_funnel(session: AsyncSession, name: str, description: str = "") -> Funnel:
    normalized_name = " ".join(name.split())
    normalized_description = description.strip()
    if not normalized_name:
        raise ValueError("Название воронки обязательно.")
    if len(normalized_name) > 255:
        raise ValueError("Название воронки не может быть длиннее 255 символов.")
    if len(normalized_description) > 500:
        raise ValueError("Описание воронки не может быть длиннее 500 символов.")
    existing = await session.scalar(
        select(Funnel).where(func.casefold(Funnel.name) == normalized_name.casefold())
    )
    if existing:
        raise FunnelAlreadyExists("Воронка с таким названием уже существует.")
    funnel = Funnel(name=normalized_name, description=normalized_description or None)
    session.add(funnel)
    await session.flush()
    return funnel
