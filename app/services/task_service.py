from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.models import ActivityLog, Client, ClientFunnel, Comment, Funnel, User


def parse_optional_date(raw_value: str | None) -> date | None:
    if not raw_value:
        return None
    return date.fromisoformat(raw_value)


def parse_decimal(raw_value: str | None) -> Decimal | None:
    if not raw_value:
        return None
    normalized = raw_value.replace(" ", "").replace(",", ".")
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


async def get_funnels(session: AsyncSession) -> list[Funnel]:
    result = await session.execute(select(Funnel).order_by(Funnel.name))
    return list(result.scalars())


async def get_managers(session: AsyncSession) -> list[User]:
    result = await session.execute(select(User).where(User.role == "manager").order_by(User.full_name))
    return list(result.scalars())


async def get_funnel_by_id(funnel_id: int, session: AsyncSession) -> Funnel | None:
    result = await session.execute(select(Funnel).where(Funnel.id == funnel_id))
    return result.scalar_one_or_none()


async def get_funnel_clients(
    funnel_id: int,
    current_user: User,
    session: AsyncSession,
) -> list[Client]:
    statement: Select[tuple[Client]] = (
        select(Client)
        .join(ClientFunnel, ClientFunnel.client_id == Client.id)
        .where(ClientFunnel.funnel_id == funnel_id)
        .options(
            joinedload(Client.assigned_manager),
            selectinload(Client.funnel_links).joinedload(ClientFunnel.funnel),
            selectinload(Client.comments).joinedload(Comment.author),
        )
        .order_by(Client.updated_at.desc(), Client.full_name.asc())
    )
    if current_user.role != "boss":
        statement = statement.where(Client.assigned_manager_id == current_user.id)
    result = await session.execute(statement)
    return list(result.scalars().unique())


async def get_client_with_details(client_id: int, session: AsyncSession) -> Client | None:
    result = await session.execute(
        select(Client)
        .where(Client.id == client_id)
        .options(
            joinedload(Client.assigned_manager),
            selectinload(Client.funnel_links).joinedload(ClientFunnel.funnel),
            selectinload(Client.comments).joinedload(Comment.author),
        )
    )
    return result.scalar_one_or_none()


async def get_activity_feed(current_user: User, session: AsyncSession) -> list[ActivityLog]:
    today = date.today()
    start = datetime.combine(today, time.min)
    end = datetime.combine(today, time.max)
    statement = (
        select(ActivityLog)
        .where(ActivityLog.created_at >= start, ActivityLog.created_at <= end)
        .options(joinedload(ActivityLog.client), joinedload(ActivityLog.actor))
        .order_by(ActivityLog.created_at.desc())
    )
    if current_user.role != "boss":
        statement = statement.join(Client, Client.id == ActivityLog.client_id).where(
            Client.assigned_manager_id == current_user.id
        )
    result = await session.execute(statement)
    return list(result.scalars().unique())


async def update_client_task(
    client: Client,
    form_data: dict[str, str],
    actor: User,
    session: AsyncSession,
) -> None:
    tracked_fields = {
        "status": ("status", form_data.get("status", "").strip()),
        "phone": ("phone", form_data.get("phone", "").strip()),
        "last_call_date": ("last_call_date", parse_optional_date(form_data.get("last_call_date"))),
        "next_call_date": ("next_call_date", parse_optional_date(form_data.get("next_call_date"))),
        "manager_notes": ("manager_notes", form_data.get("manager_notes", "").strip()),
        "company_name": ("company_name", form_data.get("company_name", "").strip()),
        "monthly_turnover": ("monthly_turnover", parse_decimal(form_data.get("monthly_turnover"))),
        "mcc_code": ("mcc_code", form_data.get("mcc_code", "").strip()),
        "competitor_bank": ("competitor_bank", form_data.get("competitor_bank", "").strip()),
        "registration_address": ("registration_address", form_data.get("registration_address", "").strip()),
    }

    for field_name, (attribute, new_value) in tracked_fields.items():
        old_value = getattr(client, attribute)
        old_text = "" if old_value is None else str(old_value)
        new_text = "" if new_value is None else str(new_value)
        if old_text == new_text:
            continue
        setattr(client, attribute, new_value)
        session.add(
            ActivityLog(
                client_id=client.id,
                actor_id=actor.id,
                field_name=field_name,
                previous_value=old_text,
                new_value=new_text,
            )
        )

    comment_body = form_data.get("comment_body", "").strip()
    if comment_body:
        session.add(Comment(client_id=client.id, author_id=actor.id, body=comment_body))
        session.add(
            ActivityLog(
                client_id=client.id,
                actor_id=actor.id,
                field_name="comment",
                previous_value="",
                new_value=comment_body,
            )
        )

    manager_id_raw = form_data.get("assigned_manager_id")
    if actor.role == "boss" and manager_id_raw:
        new_manager_id = int(manager_id_raw)
        old_manager_id = client.assigned_manager_id
        if old_manager_id != new_manager_id:
            old_manager = await session.get(User, old_manager_id) if old_manager_id else None
            new_manager = await session.get(User, new_manager_id)
            client.assigned_manager_id = new_manager_id
            session.add(
                ActivityLog(
                    client_id=client.id,
                    actor_id=actor.id,
                    field_name="assigned_manager",
                    previous_value=old_manager.full_name if old_manager else "",
                    new_value=new_manager.full_name if new_manager else "",
                )
            )


async def get_dashboard_stats(current_user: User, session: AsyncSession) -> dict[str, int]:
    base = select(func.count(Client.id))
    if current_user.role != "boss":
        base = base.where(Client.assigned_manager_id == current_user.id)

    total_clients = (await session.execute(base)).scalar_one()
    total_funnels = (await session.execute(select(func.count(Funnel.id)))).scalar_one()
    multi_funnel = (
        await session.execute(
            select(func.count())
            .select_from(
                select(ClientFunnel.client_id)
                .group_by(ClientFunnel.client_id)
                .having(func.count(ClientFunnel.funnel_id) > 1)
                .subquery()
            )
        )
    ).scalar_one()
    activity_today = len(await get_activity_feed(current_user=current_user, session=session))
    return {
        "total_clients": total_clients,
        "total_funnels": total_funnels,
        "multi_funnel": multi_funnel,
        "activity_today": activity_today,
    }


def client_to_export_row(client: Client) -> dict[str, str]:
    funnel_names = ", ".join(sorted(link.funnel.name for link in client.funnel_links))
    return {
        "ФИО": client.full_name,
        "Организация": client.company_name,
        "ИНН": client.tax_number,
        "Телефон": client.phone,
        "Комментарий менеджера": client.manager_notes,
        "Дата звонка": client.last_call_date.isoformat() if client.last_call_date else "",
        "Следующая дата звонка": client.next_call_date.isoformat() if client.next_call_date else "",
        "Статус": client.status,
        "Оборот в месяц": str(client.monthly_turnover or ""),
        "MCC": client.mcc_code,
        "Банк-конкурент": client.competitor_bank,
        "Адрес регистрации": client.registration_address,
        "Воронки": funnel_names,
        "Менеджер": client.assigned_manager.full_name if client.assigned_manager else "",
        "Табельный номер": client.assigned_manager.employee_id if client.assigned_manager else "",
    }
