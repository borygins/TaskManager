from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook, load_workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Client, ClientFunnel, Funnel, User
from app.services.task_service import client_to_export_row, parse_decimal, parse_optional_date

EXPORT_HEADERS = [
    "ФИО",
    "Организация",
    "ИНН",
    "Телефон",
    "Комментарий менеджера",
    "Дата звонка",
    "Следующая дата звонка",
    "Дата последней активности менеджера",
    "Статус",
    "Оборот в месяц",
    "MCC",
    "Банк-конкурент",
    "Адрес регистрации",
    "Воронки",
    "Табельный номер",
    "ФИО менеджера",
]


async def export_clients_to_workbook(session: AsyncSession, manager_id: int | None = None) -> bytes:
    statement = select(Client).options(
        selectinload(Client.funnel_links).joinedload(ClientFunnel.funnel),
        selectinload(Client.assigned_manager),
    )
    if manager_id:
        statement = statement.where(Client.assigned_manager_id == manager_id)
    statement = statement.order_by(Client.updated_at.desc())
    result = await session.execute(statement)
    clients = list(result.scalars().unique())

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Задачи"
    sheet.append(EXPORT_HEADERS)
    for client in clients:
        row = client_to_export_row(client)
        sheet.append([row.get(header, "") for header in EXPORT_HEADERS])

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


async def import_clients_from_excel(file_bytes: bytes, session: AsyncSession) -> dict[str, int]:
    workbook = load_workbook(filename=BytesIO(file_bytes))
    sheet = workbook.active
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return {"created": 0, "updated": 0}

    headers = [str(value).strip() if value is not None else "" for value in rows[0]]
    header_index = {header: idx for idx, header in enumerate(headers)}

    created = 0
    updated = 0

    for raw_row in rows[1:]:
        tax_number = str(raw_row[header_index["ИНН"]] or "").strip()
        if not tax_number:
            continue

        manager_column = header_index.get("Табельный номер")
        manager_employee_id = str(raw_row[manager_column] or "").strip() if manager_column is not None else ""
        manager = None
        if manager_employee_id:
            manager_result = await session.execute(select(User).where(User.employee_id == manager_employee_id))
            manager = manager_result.scalar_one_or_none()

        last_activity_column = header_index.get("Дата последней активности менеджера")
        last_activity_date = None
        if last_activity_column is not None:
            last_activity_date = parse_optional_date(str(raw_row[last_activity_column] or "").strip())

        result = await session.execute(select(Client).where(Client.tax_number == tax_number))
        client = result.scalar_one_or_none()
        is_new = client is None
        if is_new:
            client = Client(
                full_name=str(raw_row[header_index["ФИО"]] or "").strip(),
                company_name=str(raw_row[header_index["Организация"]] or "").strip(),
                tax_number=tax_number,
                phone=str(raw_row[header_index["Телефон"]] or "").strip(),
                manager_notes=str(raw_row[header_index["Комментарий менеджера"]] or "").strip(),
                last_call_date=parse_optional_date(str(raw_row[header_index["Дата звонка"]] or "").strip()),
                next_call_date=parse_optional_date(str(raw_row[header_index["Следующая дата звонка"]] or "").strip()),
                last_manager_activity_date=last_activity_date,
                status=str(raw_row[header_index["Статус"]] or "Поиск контакта").strip(),
                monthly_turnover=parse_decimal(str(raw_row[header_index["Оборот в месяц"]] or "").strip()),
                mcc_code=str(raw_row[header_index["MCC"]] or "").strip(),
                competitor_bank=str(raw_row[header_index["Банк-конкурент"]] or "").strip(),
                registration_address=str(raw_row[header_index["Адрес регистрации"]] or "").strip(),
                assigned_manager_id=manager.id if manager else None,
            )
            session.add(client)
            await session.flush()
            created += 1
        else:
            client.full_name = str(raw_row[header_index["ФИО"]] or "").strip()
            client.company_name = str(raw_row[header_index["Организация"]] or "").strip()
            client.phone = str(raw_row[header_index["Телефон"]] or "").strip()
            client.manager_notes = str(raw_row[header_index["Комментарий менеджера"]] or "").strip()
            client.last_call_date = parse_optional_date(str(raw_row[header_index["Дата звонка"]] or "").strip())
            client.next_call_date = parse_optional_date(str(raw_row[header_index["Следующая дата звонка"]] or "").strip())
            client.last_manager_activity_date = last_activity_date
            client.status = str(raw_row[header_index["Статус"]] or "Поиск контакта").strip()
            client.monthly_turnover = parse_decimal(str(raw_row[header_index["Оборот в месяц"]] or "").strip())
            client.mcc_code = str(raw_row[header_index["MCC"]] or "").strip()
            client.competitor_bank = str(raw_row[header_index["Банк-конкурент"]] or "").strip()
            client.registration_address = str(raw_row[header_index["Адрес регистрации"]] or "").strip()
            if manager:
                client.assigned_manager_id = manager.id
            updated += 1

        funnel_names = str(raw_row[header_index["Воронки"]] or "").strip()
        if funnel_names:
            for name in [item.strip() for item in funnel_names.split(",") if item.strip()]:
                funnel_result = await session.execute(select(Funnel).where(Funnel.name == name))
                funnel = funnel_result.scalar_one_or_none()
                if not funnel:
                    funnel = Funnel(name=name)
                    session.add(funnel)
                    await session.flush()
                exists_result = await session.execute(
                    select(ClientFunnel).where(ClientFunnel.client_id == client.id, ClientFunnel.funnel_id == funnel.id)
                )
                if not exists_result.scalar_one_or_none():
                    session.add(ClientFunnel(client_id=client.id, funnel_id=funnel.id))

    return {"created": created, "updated": updated}


def build_import_template() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Шаблон импорта"
    sheet.append(EXPORT_HEADERS)
    sheet.append(
        [
            "Иванов Иван Иванович",
            "ООО Ромашка",
            "7701234567",
            "+79990000000",
            "Первичный контакт",
            "2026-03-25",
            "2026-03-28",
            "2026-03-25",
            "Поиск контакта",
            "1500000",
            "5411",
            "Банк N",
            "Москва, ул. Примерная, д. 1",
            "Доля рынка, Спарк",
            "01666222",
            "Тестовый менеджер",
        ]
    )
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
