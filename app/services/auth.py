from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import find_user_by_employee_id
from app.core.security import verify_password
from app.models import User


async def authenticate_user(
    employee_id: str,
    password: str,
    session: AsyncSession,
) -> User | None:
    user = await find_user_by_employee_id(employee_id=employee_id, session=session)
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user
