from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import find_user_by_employee_id
from app.core.security import normalize_employee_id, verify_password
from app.models import User


async def authenticate_user(
    employee_id: str,
    password: str,
    session: AsyncSession,
) -> User | None:
    try:
        normalized_employee_id = normalize_employee_id(employee_id)
    except ValueError:
        return None
    user = await find_user_by_employee_id(employee_id=normalized_employee_id, session=session)
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user
