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


def build_change_message(label: str, old_value: object, new_value: object) -> str:
    old_text = "" if old_value is None else str(old_value)
    new_text = "" if new_value is None else str(new_value)
    return f"{label}: {old_text} -> {new_text}" if old_text else f"{label}: {new_text}"


async def append_daily_activity(client_id: int, actor: User, changes: list[str], session: AsyncSession) -> None:
    if not changes:
        return

    now = datetime.now()
    start = datetime.combine(now.date(), time.min)
    end = datetime.combine(now.date(), time.max)
    result = await session.execute(
        select(ActivityLog)
        .where(ActivityLog.client_id == client_id, ActivityLog.created_at >= start, ActivityLog.created_at <= end)
        .order_by(ActivityLog.created_at.desc())
        .limit(1)
    )
    log = result.scalar_one_or_none()
    chunk = "\n".join(f"{now.strftime('%H:%M')} - {actor.full_name}: {change}" for change in changes)

    if log:
        log.new_value = f"{log.new_value}\n{chunk}" if log.new_value else chunk
        log.actor_id = actor.id
        log.created_at = now
        return

    session.add(
        ActivityLog(
            client_id=client_id,
            actor_id=actor.id,
            field_name="daily_summary",
            previous_value="",
            new_value=chunk,
            created_at=now,
        )
    )


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
    inn_query: str | None = None,
    status_filter: str | None = None,
    next_call_date_filter: date | None = None,
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
    if inn_query:
        statement = statement.where(Client.tax_number.contains(inn_query.strip()))
    if status_filter:
        statement = statement.where(Client.status == status_filter)
    if next_call_date_filter:
        statement = statement.where(Client.next_call_date == next_call_date_filter)
    result = await session.execute(statement)
    return list(result.scalars().unique())


async def get_clients_for_distribution(
    session: AsyncSession,
    inn_query: str | None = None,
    status_filter: str | None = None,
    manager_id: int | None = None,
) -> list[Client]:
    statement: Select[tuple[Client]] = (
        select(Client)
        .options(
            joinedload(Client.assigned_manager),
            selectinload(Client.funnel_links).joinedload(ClientFunnel.funnel),
            selectinload(Client.comments).joinedload(Comment.author),
        )
        .order_by(Client.updated_at.desc(), Client.full_name.asc())
    )
    if inn_query:
        statement = statement.where(Client.tax_number.contains(inn_query.strip()))
    if status_filter:
        statement = statement.where(Client.status == status_filter)
    if manager_id:
        statement = statement.where(Client.assigned_manager_id == manager_id)
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
    tracked_fields = [
        ("status", "status", "Статус", lambda value: value.strip()),
        ("phone", "phone", "Телефон", lambda value: value.strip()),
        ("last_call_date", "last_call_date", "Дата звонка", parse_optional_date),
        ("next_call_date", "next_call_date", "Следующая дата звонка", parse_optional_date),
        ("company_name", "company_name", "Организация", lambda value: value.strip()),
        ("monthly_turnover", "monthly_turnover", "Оборот в месяц", parse_decimal),
        ("mcc_code", "mcc_code", "MCC", lambda value: value.strip()),
        ("competitor_bank", "competitor_bank", "Банк-конкурент", lambda value: value.strip()),
        ("registration_address", "registration_address", "Адрес регистрации", lambda value: value.strip()),
    ]

    changes: list[str] = []

    for form_key, attribute, label, parser in tracked_fields:
        if form_key not in form_data:
            continue
        raw_value = form_data.get(form_key)
        new_value = parser(raw_value)
        old_value = getattr(client, attribute)
        old_text = "" if old_value is None else str(old_value)
        new_text = "" if new_value is None else str(new_value)
        if old_text == new_text:
            continue
        setattr(client, attribute, new_value)
        changes.append(build_change_message(label=label, old_value=old_value, new_value=new_value))

    comment_body = form_data.get("comment_body", "").strip()
    if comment_body:
        session.add(Comment(client_id=client.id, author_id=actor.id, body=comment_body))
        changes.append(f"Комментарий к активности: {comment_body}")

    manager_id_raw = form_data.get("assigned_manager_id")
    if actor.role == "boss" and manager_id_raw:
        new_manager_id = int(manager_id_raw)
        old_manager_id = client.assigned_manager_id
        if old_manager_id != new_manager_id:
            old_manager = await session.get(User, old_manager_id) if old_manager_id else None
            new_manager = await session.get(User, new_manager_id)
            client.assigned_manager_id = new_manager_id
            changes.append(
                build_change_message(
                    label="Ответственный менеджер",
                    old_value=old_manager.full_name if old_manager else "",
                    new_value=new_manager.full_name if new_manager else "",
                )
            )

    if changes:
        client.last_manager_activity_date = date.today()
        await append_daily_activity(client_id=client.id, actor=actor, changes=changes, session=session)


async def bulk_assign_clients(
    clients: list[Client],
    new_manager: User,
    actor: User,
    session: AsyncSession,
) -> int:
    updated = 0
    for client in clients:
        if client.assigned_manager_id == new_manager.id:
            continue
        old_manager = client.assigned_manager.full_name if client.assigned_manager else ""
        client.assigned_manager_id = new_manager.id
        client.last_manager_activity_date = date.today()
        await append_daily_activity(
            client_id=client.id,
            actor=actor,
            changes=[build_change_message("Ответственный менеджер", old_manager, new_manager.full_name)],
            session=session,
        )
        updated += 1
    return updated


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
        "Дата последней активности менеджера": (
            client.last_manager_activity_date.isoformat() if client.last_manager_activity_date else ""
        ),
        "Статус": client.status,
        "Оборот в месяц": str(client.monthly_turnover or ""),
        "MCC": client.mcc_code,
        "Банк-конкурент": client.competitor_bank,
        "Адрес регистрации": client.registration_address,
        "Воронки": funnel_names,
        "Табельный номер": client.assigned_manager.employee_id if client.assigned_manager else "",
        "ФИО менеджера": client.assigned_manager.full_name if client.assigned_manager else "",
    }
