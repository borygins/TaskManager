from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_password
from app.models import Funnel, User


async def seed_defaults(session: AsyncSession) -> None:
    for funnel_name in settings.funnel_names:
        result = await session.execute(select(Funnel).where(Funnel.name == funnel_name))
        funnel = result.scalar_one_or_none()
        if not funnel:
            session.add(Funnel(name=funnel_name, description=f"Продажная кампания {funnel_name}"))

    for employee_id, payload in settings.seeded_users.items():
        result = await session.execute(select(User).where(User.employee_id == employee_id))
        user = result.scalar_one_or_none()
        if not user:
            session.add(
                User(
                    employee_id=employee_id,
                    full_name=payload["full_name"],
                    role=payload["role"],
                    password_hash=hash_password(payload["password"]),
                )
            )
