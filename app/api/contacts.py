from __future__ import annotations

import asyncio
import calendar
import secrets
from datetime import date, timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user, get_manager_user
from app.core.config import settings
from app.core.sales import ACTIVITY_MARKS, CLIENT_STATUSES, FINAL_CLIENT_STATUSES
from app.db.session import SessionLocal
from app.models import Client, ClientFunnel, ContactActivity, ContactReportState, User
from app.services.contact_reports import (
    REPORT_HEADERS,
    build_contact_workbook,
    contact_report_content_disposition,
    generate_daily_report,
)
from app.services.contacts import aggregate_contacts, list_contacts, parse_contact, record_contact
from app.services.task_service import get_client_with_details


router = APIRouter()
templates = Jinja2Templates(directory=str(settings.templates_dir))
signer = URLSafeTimedSerializer(settings.secret_key, salt="contact-submission")


def csrf_token(request: Request) -> str:
    if "contact_csrf" not in request.session:
        request.session["contact_csrf"] = secrets.token_urlsafe(32)
    return request.session["contact_csrf"]


def check_csrf(request: Request, form: dict[str, str]) -> None:
    expected = request.session.get("contact_csrf", "")
    if not expected or not secrets.compare_digest(expected, form.get("csrf_token", "")):
        raise HTTPException(403, "Форма устарела. Обновите страницу и повторите отправку.")


def render(request: Request, template: str, context: dict, status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name=template, context={
        "csrf_token": csrf_token(request), **context,
    }, status_code=status_code)


def parse_day(raw: str | None) -> date:
    try:
        return date.fromisoformat(raw) if raw else date.today()
    except ValueError as exc:
        raise HTTPException(400, "Некорректная дата.") from exc


def month_bounds(raw: str | None) -> tuple[date, date]:
    try:
        raw = raw or date.today().strftime("%Y-%m")
        start = date.fromisoformat(raw + "-01")
        end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
        return start, end
    except ValueError as exc:
        raise HTTPException(400, "Некорректный месяц.") from exc


async def report_manager(session, user: User, manager_id: int | None) -> User:
    if user.role != "boss":
        if manager_id is not None and manager_id != user.id:
            raise HTTPException(403, "Можно выгружать только свой журнал.")
        return user
    if manager_id is None:
        raise HTTPException(400, "Выберите менеджера.")
    manager = await session.get(User, manager_id)
    if not manager:
        raise HTTPException(404, "Менеджер не найден.")
    if manager.role != "manager":
        history = await session.scalar(select(ContactActivity.id).where(ContactActivity.manager_id == manager.id).limit(1))
        if history is None:
            raise HTTPException(403, "У выбранного пользователя нет журнала менеджера.")
    return manager


async def report_managers(session) -> list[User]:
    """Managers available for current and historical contact reports."""
    has_history = select(ContactActivity.id).where(ContactActivity.manager_id == User.id).exists()
    return list((await session.execute(
        select(User).where(or_(User.role == "manager", has_history)).order_by(User.full_name)
    )).scalars())


async def form_response(request: Request, client_id: int, user: User, values=None, error=None, status_code=200):
    async with SessionLocal() as session:
        client = await get_client_with_details(client_id, session)
        if not client:
            raise HTTPException(404, "Клиент не найден.")
        if client.assigned_manager_id != user.id:
            raise HTTPException(403, "Нет доступа к клиенту.")
    if values is None:
        values = {
            "activity_date": date.today().isoformat(), "activity_type": "Звонок",
            "client_status": client.status,
            "comment": "", "next_activity_date": "",
        }
    values = dict(values)
    values.setdefault("submission_token", signer.dumps({
        "key": uuid4().hex, "manager": user.id, "client": client.id,
    }))
    return render(request, "contact_form.html", {
        "current_user": user, "client": client,
        "values": values, "error": error,
        "statuses": CLIENT_STATUSES, "marks": ACTIVITY_MARKS,
        "today": date.today().isoformat(),
    }, status_code)


@router.get("/clients/{client_id}/contacts/new", response_class=HTMLResponse)
async def contact_form(request: Request, client_id: int, user: User = Depends(get_manager_user)):
    return await form_response(request, client_id, user)


@router.post("/clients/{client_id}/contacts")
async def create_contact(request: Request, client_id: int, user: User = Depends(get_manager_user)):
    form = {key: str(value) for key, value in (await request.form()).items()}
    check_csrf(request, form)
    try:
        token = signer.loads(form.get("submission_token", ""), max_age=86400)
        if token["manager"] != user.id or token["client"] != client_id:
            raise BadSignature("Wrong form owner")
    except (BadSignature, KeyError, TypeError) as exc:
        raise HTTPException(400, "Форма устарела. Откройте новую форму записи контакта.") from exc
    try:
        data = parse_contact(form)
        async with SessionLocal() as session:
            client = await session.get(Client, client_id)
            if not client:
                raise HTTPException(404, "Клиент не найден.")
            contact = await record_contact(session, client, user, data, token["key"])
            contact_day = contact.activity_date
            await session.commit()
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        return await form_response(request, client_id, user, form, str(exc), 400)
    except IntegrityError:
        # Two requests from the same form can arrive before either commits.
        async with SessionLocal() as session:
            existing = (await session.execute(select(ContactActivity).where(
                ContactActivity.request_key == token["key"],
                ContactActivity.manager_id == user.id, ContactActivity.client_id == client_id,
            ))).scalar_one_or_none()
            if existing is None:
                raise
            contact_day = existing.activity_date
    ready = await generate_daily_report(user.id, contact_day)
    return RedirectResponse(
        f"/contacts?day={contact_day.isoformat()}&saved=1&report={'ready' if ready else 'pending'}", status_code=303,
    )


@router.get("/contacts", response_class=HTMLResponse)
async def contacts_page(request: Request, day: str | None = None, q: str = "", user: User = Depends(get_manager_user)):
    selected_day = parse_day(day)
    async with SessionLocal() as session:
        rows = aggregate_contacts(await list_contacts(session, user.id, selected_day, selected_day))
        base = select(Client).options(
            selectinload(Client.comments),
            selectinload(Client.funnel_links).joinedload(ClientFunnel.funnel),
        ).where(Client.assigned_manager_id == user.id)
        active_base = base.where(Client.status.not_in(FINAL_CLIENT_STATUSES))
        due = list((await session.execute(active_base.where(Client.next_call_date == selected_day).order_by(Client.company_name))).scalars())
        overdue = list((await session.execute(active_base.where(Client.next_call_date < selected_day).order_by(Client.next_call_date, Client.company_name))).scalars())
        unscheduled = list((await session.execute(
            active_base.where(Client.next_call_date.is_(None)).order_by(Client.company_name, Client.id)
        )).scalars())
        found = []
        if q.strip():
            term = q.strip().casefold()
            found = list((await session.execute(base.where(or_(
                func.casefold(Client.tax_number).contains(term, autoescape=True),
                func.casefold(Client.company_name).contains(term, autoescape=True),
                func.casefold(Client.full_name).contains(term, autoescape=True),
            )).order_by(Client.company_name).limit(100))).scalars())
        report_state = await session.get(ContactReportState, (user.id, selected_day))
    return render(request, "contacts.html", {
        "current_user": user, "day": selected_day, "today": date.today(), "q": q,
        "previous_day": selected_day - timedelta(days=1),
        "next_day": selected_day + timedelta(days=1),
        "rows": rows, "due": due, "overdue": overdue, "found": found, "unscheduled": unscheduled,
        "completed": sum(row.qualified for row in rows), "headers": REPORT_HEADERS,
        "report_state": report_state,
    })


@router.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request, user: User = Depends(get_current_user)):
    async with SessionLocal() as session:
        managers = await report_managers(session) if user.role == "boss" else []
    return render(request, "reports.html", {
        "current_user": user, "today": date.today(), "managers": managers,
    })


@router.get("/contacts/journal", response_class=HTMLResponse)
async def contact_journal(request: Request, month: str | None = None, manager_id: int | None = None,
                          user: User = Depends(get_current_user)):
    start, end = month_bounds(month)
    async with SessionLocal() as session:
        # Include former managers so their historical reports remain accessible.
        managers = await report_managers(session) if user.role == "boss" else []
        manager = None
        rows = []
        if user.role != "boss" or manager_id is not None:
            manager = await report_manager(session, user, manager_id)
            rows = aggregate_contacts(await list_contacts(session, manager.id, start, end))
    return render(request, "contact_journal.html", {
        "current_user": user, "month": start.strftime("%Y-%m"), "managers": managers,
        "selected_manager": manager, "rows": rows, "headers": REPORT_HEADERS,
    })


@router.get("/contacts/export")
async def export_contacts(day: str | None = None, month: str | None = None, manager_id: int | None = None,
                          preview: bool = False,
                          user: User = Depends(get_current_user)):
    if day and month:
        raise HTTPException(400, "Выберите день или месяц, не оба периода одновременно.")
    start, end = month_bounds(month) if month else (parse_day(day), parse_day(day))
    async with SessionLocal() as session:
        manager = await report_manager(session, user, manager_id)
        rows = aggregate_contacts(await list_contacts(session, manager.id, start, end))
    if preview:
        return JSONResponse({"count": len(rows)}, headers={"Cache-Control": "no-store"})
    period = start.strftime("%m.%Y" if month else "%d.%m.%Y")
    content = await asyncio.to_thread(build_contact_workbook, rows, manager.full_name, period)
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={
        "Content-Disposition": contact_report_content_disposition(
            manager.employee_id, start, monthly=bool(month),
        ),
    })


@router.post("/contacts/report/retry")
async def retry_report(request: Request, user: User = Depends(get_manager_user)):
    form = {key: str(value) for key, value in (await request.form()).items()}
    check_csrf(request, form)
    day = parse_day(form.get("day"))
    await generate_daily_report(user.id, day)
    return RedirectResponse(f"/contacts?day={day.isoformat()}", status_code=303)
