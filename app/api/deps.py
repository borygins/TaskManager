from __future__ import annotations

from datetime import date

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.models import User


async def normalize_user_role(user: User, session: AsyncSession) -> User:
    if user.role_expires_at and user.role_after_expiry and user.role_expires_at < date.today():
        user.role = user.role_after_expiry
        user.role_after_expiry = None
        user.role_expires_at = None
        await session.commit()
        await session.refresh(user)
    return user


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    user = await session.get(User, user_id)
    if not user or not user.is_active:
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    user = await normalize_user_role(user=user, session=session)
    if user.must_change_password and request.url.path != "/change-password":
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/change-password"})
    return user


async def get_boss_user(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "boss":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Требуются права начальника.")
    return current_user


async def find_user_by_employee_id(employee_id: str, session: AsyncSession) -> User | None:
    result = await session.execute(select(User).where(User.employee_id == employee_id))
    return result.scalar_one_or_none()
