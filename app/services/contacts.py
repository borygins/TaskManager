from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Client, Comment, ContactActivity, ContactReportState, Funnel, User
from app.services.task_service import append_daily_activity


CONTACT_RESULTS = (
    "Недозвон", "Перенос на дату", "Поиск контакта", "Отказ", "Переговоры",
    "Согласие", "Мискодинг", "Договор подписан", "Договор запущен",
)
STAGE_BY_RESULT = {
    "Поиск контакта": "Поиск контакта", "Отказ": "Отказ", "Переговоры": "Переговоры",
    "Согласие": "Согласие", "Мискодинг": "Мискодинг",
    "Договор подписан": "Подписан", "Договор запущен": "Подписан",
}


@dataclass(frozen=True)
class ContactInput:
    activity_date: date
    activity_type: str
    result: str
    potential_turnover: Decimal
    next_activity_date: date | None
    comment: str
    funnel_id: int


def parse_contact(form: dict[str, str]) -> ContactInput:
    try:
        day = date.fromisoformat(form.get("activity_date", ""))
        next_raw = form.get("next_activity_date", "").strip()
        next_day = date.fromisoformat(next_raw) if next_raw else None
        funnel_id = int(form.get("funnel_id", ""))
    except (ValueError, TypeError) as exc:
        raise ValueError("Укажите корректную дату активности, следующую дату и воронку.") from exc
    if day > date.today():
        raise ValueError("Нельзя записывать выполненный контакт будущей датой.")
    if next_day and next_day < day:
        raise ValueError("Следующая дата не может быть раньше даты активности.")
    kind = form.get("activity_type", "")
    result = form.get("result", "")
    if kind not in {"Звонок", "Встреча"} or result not in CONTACT_RESULTS:
        raise ValueError("Выберите вид активности и результат из списка.")
    if kind == "Встреча" and result == "Недозвон":
        raise ValueError("Для встречи нельзя выбрать результат «Недозвон».")
    if result in {"Недозвон", "Перенос на дату"} and not next_day:
        raise ValueError("При недозвоне или переносе обязательно укажите следующую дату.")
    comment = form.get("comment", "").strip()
    if not comment:
        raise ValueError("Напишите комментарий к активности.")
    if len(comment) > 10000:
        raise ValueError("Комментарий не должен превышать 10 000 символов.")
    try:
        amount = Decimal(form.get("potential_turnover", "000000").replace(" ", "").replace(",", "."))
        if not amount.is_finite() or amount < 0 or amount > Decimal("999999999999.99"):
            raise ValueError
        if amount != amount.quantize(Decimal("0.01")):
            raise ValueError
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Потенциал оборота: число от 0 до 999999999999,99, не более двух знаков после запятой.") from exc
    return ContactInput(day, kind, result, amount, next_day, comment, funnel_id)


async def record_contact(
    session: AsyncSession, client: Client, actor: User, data: ContactInput, request_key: str,
) -> ContactActivity:
    if actor.role != "boss" and client.assigned_manager_id != actor.id:
        raise PermissionError("Нет доступа к клиенту.")
    existing = (await session.execute(
        select(ContactActivity).where(ContactActivity.request_key == request_key)
    )).scalar_one_or_none()
    if existing:
        if existing.manager_id != actor.id or existing.client_id != client.id:
            raise PermissionError("Повторная отправка не соответствует клиенту или пользователю.")
        return existing
    funnel = await session.get(Funnel, data.funnel_id)
    if not funnel:
        raise ValueError("Выбранная воронка больше не существует.")
    latest_day = (await session.execute(
        select(func.max(ContactActivity.activity_date)).where(ContactActivity.client_id == client.id)
    )).scalar_one()
    contact = ContactActivity(
        request_key=request_key, client_id=client.id, manager_id=actor.id,
        funnel_id=funnel.id, activity_date=data.activity_date, activity_type=data.activity_type,
        dialogue=data.result != "Недозвон", result=data.result, potential_turnover=data.potential_turnover,
        next_activity_date=data.next_activity_date, comment=data.comment,
        tax_number=client.tax_number, company_name=client.company_name or client.full_name,
        funnel_name=funnel.name,
    )
    session.add(contact)
    await session.flush()
    # A historical entry must not replace a newer plan or sales stage.
    if latest_day is None or data.activity_date >= latest_day:
        client.next_call_date = data.next_activity_date
        if data.result in STAGE_BY_RESULT:
            client.status = STAGE_BY_RESULT[data.result]
    if data.activity_type == "Звонок":
        client.last_call_date = max(client.last_call_date or data.activity_date, data.activity_date)
    client.last_manager_activity_date = max(
        client.last_manager_activity_date or data.activity_date, data.activity_date,
    )
    session.add(Comment(client_id=client.id, author_id=actor.id, body=data.comment))
    await append_daily_activity(
        client.id, actor,
        [f"{data.activity_date:%d.%m.%Y} — {data.activity_type}: {data.result}. {data.comment}"], session,
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
        ).order_by(ContactActivity.activity_date, ContactActivity.created_at, ContactActivity.id)
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
        rows[key] = ContactRow(
            client_id=item.client_id, manager_id=item.manager_id, day=item.activity_date,
            tax_number=item.tax_number, company_name=item.company_name,
            potential=item.potential_turnover, channel=item.funnel_name, status=item.result,
            kind="Встреча" if item.activity_type == "Встреча" or (previous and previous.kind == "Встреча") else "Звонок",
            next_day=item.next_activity_date,
            comment=f"{previous.comment} = {item.comment}" if previous else item.comment,
            qualified=bool(item.result != "Недозвон" or (previous and previous.qualified)),
        )
    return list(rows.values())
