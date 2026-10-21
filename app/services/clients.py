from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Client, ClientFunnel, Funnel, User
from app.services.task_service import append_daily_activity


@dataclass(frozen=True)
class NewClientInput:
    tax_number: str
    company_name: str
    full_name: str
    phone: str
    funnel_id: int


class ClientAlreadyExists(ValueError):
    def __init__(self, client: Client, actor: User):
        self.accessible_client_id = None
        if client.assigned_manager_id == actor.id or actor.role == "boss":
            self.accessible_client_id = client.id
            message = "Клиент с таким ИНН уже есть в базе. Откройте существующую карточку."
        else:
            message = "Клиент с таким ИНН уже есть в базе. Для доступа или назначения обратитесь к руководителю."
        super().__init__(message)


def parse_new_client(form: dict[str, str]) -> NewClientInput:
    tax_number = form.get("tax_number", "").strip()
    if not re.fullmatch(r"(?:[0-9]{10}|[0-9]{12})", tax_number):
        raise ValueError("Введите ИНН: 10 или 12 цифр без пробелов.")
    company_name = form.get("company_name", "").strip()
    full_name = form.get("full_name", "").strip()
    phone = form.get("phone", "").strip()
    if not company_name:
        raise ValueError("Укажите название организации или ФИО индивидуального предпринимателя.")
    if len(company_name) > 255 or len(full_name) > 255 or len(phone) > 64:
        raise ValueError("Название и ФИО — до 255 символов, телефон — до 64 символов.")
    try:
        funnel_id = int(form.get("funnel_id", ""))
    except ValueError as exc:
        raise ValueError("Выберите воронку из списка.") from exc
    return NewClientInput(tax_number, company_name, full_name or company_name, phone, funnel_id)


async def create_client(session: AsyncSession, actor: User, data: NewClientInput) -> Client:
    existing = (await session.execute(select(Client).where(Client.tax_number == data.tax_number))).scalar_one_or_none()
    if existing:
        raise ClientAlreadyExists(existing, actor)
    funnel = await session.get(Funnel, data.funnel_id)
    if funnel is None:
        raise ValueError("Выбранная воронка не найдена. Выберите существующую воронку.")
    client = Client(
        tax_number=data.tax_number, company_name=data.company_name, full_name=data.full_name,
        phone=data.phone, assigned_manager_id=actor.id, status="Поиск контакта",
        last_manager_activity_date=date.today(),
    )
    session.add(client)
    await session.flush()
    session.add(ClientFunnel(client_id=client.id, funnel_id=funnel.id))
    await append_daily_activity(client.id, actor, [f"Создан клиент, добавлен в воронку «{funnel.name}»."], session)
    return client
