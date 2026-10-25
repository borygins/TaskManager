from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.sales import (
    ACTIVITY_MARKS,
    ACTIVITY_MARK_CODES,
    CLIENT_STATUS_SET,
    MISSED_CALL_MARK,
    mark_labels,
)
from app.models import Client, ClientFunnel, Comment, ContactActivity, ContactReportState, Funnel, User
from app.services.task_service import append_daily_activity


@dataclass(frozen=True)
class ContactInput:
    activity_date: date
    activity_type: str
    client_status: str
    marks: tuple[str, ...]
    next_activity_date: date | None
    comment: str


def parse_contact(form: dict[str, str]) -> ContactInput:
    try:
        day = date.fromisoformat(form.get("activity_date", ""))
        next_raw = form.get("next_activity_date", "").strip()
        next_day = date.fromisoformat(next_raw) if next_raw else None
    except (ValueError, TypeError) as exc:
        raise ValueError("Укажите корректную дату активности и следующую дату.") from exc
    if day > date.today():
        raise ValueError("Нельзя записывать выполненный контакт будущей датой.")
    if next_day and next_day < day:
        raise ValueError("Следующая дата не может быть раньше даты активности.")
    kind = form.get("activity_type", "")
    client_status = form.get("client_status", "").strip()
    if kind not in {"Звонок", "Встреча"} or client_status not in CLIENT_STATUS_SET:
        raise ValueError("Выберите вид активности и статус клиента из списка.")
    submitted_marks = {
        key.removeprefix("mark_")
        for key, value in form.items()
        if key.startswith("mark_") and value == "1"
    }
    if not submitted_marks.issubset(ACTIVITY_MARK_CODES):
        raise ValueError("Выберите отметки из списка.")
    marks = tuple(code for code in ACTIVITY_MARKS if code in submitted_marks)
    missed_call = MISSED_CALL_MARK in marks
    if kind == "Встреча" and missed_call:
        raise ValueError("Для встречи нельзя выбрать отметку #недозвон.")
    if missed_call and not next_day:
        raise ValueError("При недозвоне обязательно укажите следующую дату.")
    comment = form.get("comment", "").strip()
    if not comment:
        raise ValueError("Напишите комментарий к активности.")
    if len(comment) > 10000:
        raise ValueError("Комментарий не должен превышать 10 000 символов.")
    return ContactInput(day, kind, client_status, marks, next_day, comment)


async def record_contact(
    session: AsyncSession, client: Client, actor: User, data: ContactInput, request_key: str,
) -> ContactActivity:
    if actor.role != "manager":
        raise PermissionError("Запись звонков и встреч доступна только менеджерам.")
    if client.assigned_manager_id != actor.id:
        raise PermissionError("Нет доступа к клиенту.")
    existing = (await session.execute(
        select(ContactActivity).where(ContactActivity.request_key == request_key)
    )).scalar_one_or_none()
    if existing:
        if existing.manager_id != actor.id or existing.client_id != client.id:
            raise PermissionError("Повторная отправка не соответствует клиенту или пользователю.")
        return existing
    # The earliest client-funnel link is the primary channel for the journal.
    # A client without funnels is reported as “Прочее” without creating a
    # synthetic funnel or silently changing the client's assignments.
    funnel = (await session.execute(
        select(Funnel).join(ClientFunnel, ClientFunnel.funnel_id == Funnel.id).where(
            ClientFunnel.client_id == client.id,
        ).order_by(ClientFunnel.id).limit(1)
    )).scalar_one_or_none()
    latest_day = (await session.execute(
        select(func.max(ContactActivity.activity_date)).where(ContactActivity.client_id == client.id)
    )).scalar_one()
    missed_call = MISSED_CALL_MARK in data.marks
    result = "Недозвон" if missed_call else "Контакт состоялся"
    potential_turnover = Decimal(str(client.monthly_turnover or 0))
    contact = ContactActivity(
        request_key=request_key, client_id=client.id, manager_id=actor.id,
        funnel_id=funnel.id if funnel else None, activity_date=data.activity_date, activity_type=data.activity_type,
        dialogue=not missed_call, result=result, client_status=data.client_status, marks=list(data.marks),
        potential_turnover=potential_turnover,
        next_activity_date=data.next_activity_date,
        comment_record=Comment(client_id=client.id, author_id=actor.id, body=data.comment, source="contact"),
        tax_number=client.tax_number, company_name=client.company_name or client.full_name,
        funnel_name=funnel.name if funnel else "Прочее",
    )
    session.add(contact)
    await session.flush()
    # A historical entry must not replace a newer plan or sales stage.
    if latest_day is None or data.activity_date >= latest_day:
        client.next_call_date = data.next_activity_date
        client.status = data.client_status
    if data.activity_type == "Звонок":
        client.last_call_date = max(client.last_call_date or data.activity_date, data.activity_date)
    client.last_manager_activity_date = max(
        client.last_manager_activity_date or data.activity_date, data.activity_date,
    )
    marks_text = " ".join(mark_labels(data.marks))
    summary = f"{data.activity_date:%d.%m.%Y} — {data.activity_type}: {data.client_status}."
    if marks_text:
        summary += f" {marks_text}"
    summary += f" {data.comment}"
    await append_daily_activity(
        client.id, actor,
        [summary], session,
    )
    # A durable marker allows generation to be retried after Excel locks or restarts.
    statement = insert(ContactReportState).values(
        manager_id=actor.id, activity_date=data.activity_date, revision=contact.id,
        pending=True, last_error="",
    )
    await session.execute(statement.on_conflict_do_update(
        index_elements=["manager_id", "activity_date"],
        set_={"revision": contact.id, "pending": True, "last_error": ""},
    ))
    return contact


async def list_contacts(session: AsyncSession, manager_id: int, start: date, end: date) -> list[ContactActivity]:
    result = await session.execute(
        select(ContactActivity).where(
            ContactActivity.manager_id == manager_id,
            ContactActivity.activity_date >= start, ContactActivity.activity_date <= end,
        ).options(joinedload(ContactActivity.comment_record)).order_by(
            ContactActivity.activity_date, ContactActivity.created_at, ContactActivity.id)
    )
    return list(result.scalars())


@dataclass
class ContactRow:
    client_id: int
    manager_id: int
    day: date
    tax_number: str
    company_name: str
    potential: Decimal
    channel: str
    status: str
    kind: str
    next_day: date | None
    comment: str
    qualified: bool


def aggregate_contacts(contacts: list[ContactActivity]) -> list[ContactRow]:
    rows: dict[tuple[int, date, int], ContactRow] = {}
    for item in sorted(contacts, key=lambda c: (c.activity_date, c.created_at, c.id)):
        key = (item.manager_id, item.activity_date, item.client_id)
        previous = rows.get(key)
        labels = " ".join(mark_labels(item.marks or []))
        comment = f"{labels} {item.comment_record.body}".strip()
        rows[key] = ContactRow(
            client_id=item.client_id, manager_id=item.manager_id, day=item.activity_date,
            tax_number=item.tax_number, company_name=item.company_name,
            potential=item.potential_turnover, channel=item.funnel_name, status=item.client_status,
            kind="Встреча" if item.activity_type == "Встреча" or (previous and previous.kind == "Встреча") else "Звонок",
            next_day=item.next_activity_date,
            comment=f"{previous.comment} = {comment}" if previous else comment,
            qualified=bool(MISSED_CALL_MARK not in item.marks or (previous and previous.qualified)),
        )
    return list(rows.values())
