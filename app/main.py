from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from urllib.parse import urlencode

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from starlette.middleware.sessions import SessionMiddleware

from app.api.deps import get_boss_user, get_current_user
from app.api.contacts import router as contacts_router
from app.api.clients import router as clients_router
from app.api.imports import router as imports_router
from app.core.config import settings
from app.core.sales import CLIENT_STATUSES
from app.core.security import hash_password, normalize_employee_id
from app.db.session import SessionLocal, init_db
from app.models import Client, User
from app.services.auth import authenticate_user
from app.services.backup import create_backup
from app.services.contact_reports import retry_pending_reports
from app.services.excel import build_import_template, export_clients_to_workbook
from app.services.seed import seed_defaults
from app.services.task_service import (
    bulk_assign_clients,
    get_activity_feed,
    get_client_with_details,
    get_clients_for_distribution,
    get_dashboard_stats,
    get_funnel_by_id,
    get_funnel_clients,
    get_funnels,
    get_managers,
    parse_optional_date,
    update_client_task,
)

templates = Jinja2Templates(directory=str(settings.templates_dir))
scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    await init_db()
    async with SessionLocal() as session:
        await seed_defaults(session)
        await session.commit()

    await retry_pending_reports()
    if not scheduler.running:
        scheduler.add_job(
            create_backup,
            CronTrigger(hour=settings.backup_hour, minute=0),
            id="daily-backup",
            replace_existing=True,
        )
        scheduler.add_job(
            retry_pending_reports, "interval", minutes=1,
            id="contact-report-retry", replace_existing=True, max_instances=1,
        )
        scheduler.start()

    yield

    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 12)
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")
app.include_router(contacts_router)
app.include_router(clients_router)


def render(request: Request, template_name: str, context: dict) -> HTMLResponse:
    base_context = {
        "request": request,
        "statuses": CLIENT_STATUSES,
        **context,
    }
    return templates.TemplateResponse(template_name, base_context)


@app.get("/", response_class=HTMLResponse)
async def root(request: Request) -> Response:
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return render(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_action(request: Request) -> Response:
    form = await request.form()
    raw_employee_id = str(form.get("employee_id", "")).strip()
    password = str(form.get("password", ""))
    try:
        employee_id = normalize_employee_id(raw_employee_id)
    except ValueError as exc:
        return render(request, "login.html", {"error": str(exc)})
    async with SessionLocal() as session:
        user = await authenticate_user(employee_id=employee_id, password=password, session=session)
    if not user:
        return render(request, "login.html", {"error": "Неверный табельный номер или пароль."})
    request.session["user_id"] = user.id
    if user.must_change_password:
        return RedirectResponse("/change-password", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request) -> Response:
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            request.session.clear()
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        if not user.must_change_password:
            return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return render(request, "change_password.html", {"error": None})


@app.post("/change-password", response_class=HTMLResponse)
async def change_password_action(request: Request) -> Response:
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    form = await request.form()
    password = str(form.get("password", "")).strip()
    password_confirm = str(form.get("password_confirm", "")).strip()

    if len(password) < 4:
        return render(request, "change_password.html", {"error": "Новый пароль должен быть не короче 4 символов."})
    if password != password_confirm:
        return render(request, "change_password.html", {"error": "Подтверждение пароля не совпадает."})

    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            request.session.clear()
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        user.password_hash = hash_password(password)
        user.must_change_password = False
        await session.commit()

    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> HTMLResponse:
    async with SessionLocal() as session:
        user = await session.get(User, current_user.id)
        funnels = await get_funnels(session)
        stats = await get_dashboard_stats(current_user=user, session=session)
        activities = await get_activity_feed(current_user=user, session=session)
    return render(
        request,
        "dashboard.html",
        {
            "current_user": user,
            "funnels": funnels,
            "stats": stats,
            "activities": activities[:12],
        },
    )


@app.get("/boss/users", response_class=HTMLResponse)
async def boss_users_page(
    request: Request,
    current_user: User = Depends(get_boss_user),
) -> HTMLResponse:
    async with SessionLocal() as session:
        user = await session.get(User, current_user.id)
        users = list((await session.execute(select(User).order_by(User.role.desc(), User.full_name.asc()))).scalars())
    return render(
        request,
        "boss_users.html",
        {
            "current_user": user,
            "users": users,
            "create_error": None,
        },
    )


@app.post("/boss/users")
async def create_boss_user(
    request: Request,
    current_user: User = Depends(get_boss_user),
) -> Response:
    form = await request.form()
    raw_employee_id = str(form.get("employee_id", "")).strip()
    full_name = str(form.get("full_name", "")).strip()
    role = str(form.get("role", "manager")).strip()

    try:
        employee_id = normalize_employee_id(raw_employee_id)
    except ValueError as exc:
        async with SessionLocal() as session:
            user = await session.get(User, current_user.id)
            users = list((await session.execute(select(User).order_by(User.role.desc(), User.full_name.asc()))).scalars())
        return render(
            request,
            "boss_users.html",
            {
                "current_user": user,
                "users": users,
                "create_error": str(exc),
            },
        )

    async with SessionLocal() as session:
        user = await session.get(User, current_user.id)
        users = list((await session.execute(select(User).order_by(User.role.desc(), User.full_name.asc()))).scalars())

        if not employee_id or not full_name or role not in {"boss", "manager"}:
            return render(
                request,
                "boss_users.html",
                {
                    "current_user": user,
                    "users": users,
                    "create_error": "Заполните табельный номер, ФИО и корректную роль.",
                },
            )

        existing = (await session.execute(select(User).where(User.employee_id == employee_id))).scalar_one_or_none()
        if existing:
            return render(
                request,
                "boss_users.html",
                {
                    "current_user": user,
                    "users": users,
                    "create_error": "Пользователь с таким табельным номером уже существует.",
                },
            )

        session.add(
            User(
                employee_id=employee_id,
                full_name=full_name,
                role=role,
                password_hash=hash_password(employee_id),
                must_change_password=True,
            )
        )
        await session.commit()

    return RedirectResponse("/boss/users?created=1", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/boss/users/{target_user_id}/role")
async def update_user_role(
    request: Request,
    target_user_id: int,
    current_user: User = Depends(get_boss_user),
) -> RedirectResponse:
    form = await request.form()
    new_role = str(form.get("role", "manager")).strip()
    valid_until_raw = str(form.get("role_valid_until", "")).strip()
    valid_until = parse_optional_date(valid_until_raw)

    async with SessionLocal() as session:
        _ = await session.get(User, current_user.id)
        target = await session.get(User, target_user_id)
        if target and new_role in {"boss", "manager"}:
            current_role = target.role
            previous_fallback = target.role_after_expiry
            target.role = new_role
            if valid_until:
                target.role_expires_at = valid_until
                if new_role != current_role:
                    target.role_after_expiry = current_role
                else:
                    target.role_after_expiry = previous_fallback or current_role
            else:
                target.role_expires_at = None
                target.role_after_expiry = None
            await session.commit()

    return RedirectResponse("/boss/users?role_updated=1", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/boss/users/{target_user_id}/reset-password")
async def reset_user_password(
    target_user_id: int,
    current_user: User = Depends(get_boss_user),
) -> RedirectResponse:
    async with SessionLocal() as session:
        _ = await session.get(User, current_user.id)
        target = await session.get(User, target_user_id)
        if target:
            target.password_hash = hash_password(target.employee_id)
            target.must_change_password = True
            await session.commit()
    return RedirectResponse("/boss/users?password_reset=1", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/funnels/{funnel_id}", response_class=HTMLResponse)
async def funnel_detail(
    request: Request,
    funnel_id: int,
    q: str | None = None,
    inn: str | None = None,
    status_filter: str | None = None,
    next_call_date_filter: str | None = None,
    current_user: User = Depends(get_current_user),
) -> HTMLResponse:
    search_query = q if q is not None else inn
    next_call_date = parse_optional_date(next_call_date_filter)
    async with SessionLocal() as session:
        user = await session.get(User, current_user.id)
        funnel = await get_funnel_by_id(funnel_id=funnel_id, session=session)
        if not funnel:
            raise HTTPException(status_code=404, detail="Воронка не найдена.")
        clients = await get_funnel_clients(
            funnel_id=funnel_id,
            current_user=user,
            session=session,
            search_query=search_query,
            status_filter=status_filter,
            next_call_date_filter=next_call_date,
        )
    return render(
        request,
        "funnel_detail.html",
        {
            "current_user": user,
            "funnel": funnel,
            "clients": clients,
            "search_query": search_query or "",
            "selected_status": status_filter or "",
            "selected_next_call_date": next_call_date_filter or "",
        },
    )


@app.get("/clients/{client_id}", response_class=HTMLResponse)
async def client_detail_page(
    request: Request,
    client_id: int,
    funnel_id: int | None = None,
    q: str | None = None,
    inn: str | None = None,
    status_filter: str | None = None,
    next_call_date_filter: str | None = None,
    current_user: User = Depends(get_current_user),
) -> HTMLResponse:
    search_query = q if q is not None else inn
    async with SessionLocal() as session:
        user = await session.get(User, current_user.id)
        client = await get_client_with_details(client_id=client_id, session=session)
        if not client:
            raise HTTPException(status_code=404, detail="Клиент не найден.")
        if user.role != "boss" and client.assigned_manager_id != user.id:
            raise HTTPException(status_code=403, detail="Нет доступа к клиенту.")
        managers = await get_managers(session)
    return render(
        request,
        "client_detail.html",
        {
            "current_user": user,
            "client": client,
            "managers": managers,
            "funnel_id": funnel_id,
            "search_query": search_query or "",
            "selected_status": status_filter or "",
            "selected_next_call_date": next_call_date_filter or "",
        },
    )


@app.get("/boss/distribution", response_class=HTMLResponse)
async def boss_distribution(
    request: Request,
    q: str | None = None,
    inn: str | None = None,
    status_filter: str | None = None,
    manager_id: str | None = None,
    current_user: User = Depends(get_boss_user),
) -> HTMLResponse:
    search_query = q if q is not None else inn
    manager_id_value = int(manager_id) if manager_id and manager_id.isdigit() else None
    async with SessionLocal() as session:
        user = await session.get(User, current_user.id)
        managers = await get_managers(session)
        clients = await get_clients_for_distribution(
            session=session,
            search_query=search_query,
            status_filter=status_filter,
            manager_id=manager_id_value,
        )
    return render(
        request,
        "boss_distribution.html",
        {
            "current_user": user,
            "managers": managers,
            "clients": clients,
            "search_query": search_query or "",
            "selected_status": status_filter or "",
            "selected_manager_id": manager_id_value,
        },
    )


@app.post("/boss/distribution/bulk-assign")
async def bulk_assign_distribution(
    request: Request,
    current_user: User = Depends(get_boss_user),
) -> RedirectResponse:
    form = await request.form()
    search_query = str(form.get("search_query", "")).strip()
    status_filter = str(form.get("selected_status", "")).strip()
    manager_filter_raw = str(form.get("selected_manager_id", "")).strip()
    new_manager_id_raw = str(form.get("bulk_assigned_manager_id", "")).strip()
    manager_filter = int(manager_filter_raw) if manager_filter_raw.isdigit() else None
    if not new_manager_id_raw.isdigit():
        return RedirectResponse("/boss/distribution", status_code=status.HTTP_303_SEE_OTHER)

    async with SessionLocal() as session:
        user = await session.get(User, current_user.id)
        new_manager = await session.get(User, int(new_manager_id_raw))
        clients = await get_clients_for_distribution(
            session=session,
            search_query=search_query,
            status_filter=status_filter,
            manager_id=manager_filter,
        )
        if new_manager:
            await bulk_assign_clients(clients=clients, new_manager=new_manager, actor=user, session=session)
            await session.commit()

    query = {
        key: value
        for key, value in {
            "q": search_query,
            "status_filter": status_filter,
            "manager_id": manager_filter_raw,
        }.items()
        if value
    }
    redirect_url = "/boss/distribution"
    if query:
        redirect_url = f"{redirect_url}?{urlencode(query)}"
    return RedirectResponse(redirect_url, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/boss/distribution/{client_id}/assign")
async def assign_client_to_manager(
    request: Request,
    client_id: int,
    current_user: User = Depends(get_boss_user),
) -> RedirectResponse:
    form = await request.form()
    async with SessionLocal() as session:
        user = await session.get(User, current_user.id)
        client = await get_client_with_details(client_id=client_id, session=session)
        if not client:
            raise HTTPException(status_code=404, detail="Клиент не найден.")
        await update_client_task(
            client=client,
            form_data={"assigned_manager_id": str(form.get("assigned_manager_id", ""))},
            actor=user,
            session=session,
        )
        await session.commit()

    query = {
        key: value
        for key, value in {
            "q": str(form.get("search_query", "")).strip(),
            "status_filter": str(form.get("selected_status", "")).strip(),
            "manager_id": str(form.get("selected_manager_id", "")).strip(),
        }.items()
        if value
    }
    redirect_url = "/boss/distribution"
    if query:
        redirect_url = f"{redirect_url}?{urlencode(query)}"
    return RedirectResponse(redirect_url, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/clients/{client_id}/save")
async def save_client(
    request: Request,
    client_id: int,
    current_user: User = Depends(get_current_user),
) -> RedirectResponse:
    form = await request.form()
    async with SessionLocal() as session:
        user = await session.get(User, current_user.id)
        client = await get_client_with_details(client_id=client_id, session=session)
        if not client:
            raise HTTPException(status_code=404, detail="Клиент не найден.")
        if user.role != "boss" and client.assigned_manager_id != user.id:
            raise HTTPException(status_code=403, detail="Нет доступа к клиенту.")
        try:
            await update_client_task(client=client, form_data=dict(form), actor=user, session=session)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await session.commit()

    funnel_id_raw = str(form.get("funnel_id", "")).strip()
    query = {
        key: value
        for key, value in {
            "q": str(form.get("search_query", "")).strip(),
            "status_filter": str(form.get("selected_status", "")).strip(),
            "next_call_date_filter": str(form.get("selected_next_call_date", "")).strip(),
        }.items()
        if value
    }

    if funnel_id_raw.isdigit():
        redirect_url = f"/clients/{client_id}?funnel_id={funnel_id_raw}"
        if query:
            redirect_url = f"{redirect_url}&{urlencode(query)}"
        return RedirectResponse(redirect_url, status_code=status.HTTP_303_SEE_OTHER)

    return RedirectResponse(f"/clients/{client_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/activity", response_class=HTMLResponse)
async def activity_page(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> HTMLResponse:
    async with SessionLocal() as session:
        user = await session.get(User, current_user.id)
        activities = await get_activity_feed(current_user=user, session=session)
    return render(request, "activity.html", {"current_user": user, "activities": activities})


@app.get("/export")
async def export_excel(request: Request, preview: bool = False, confirmed: bool = False,
                       current_user: User = Depends(get_current_user)) -> Response:
    async with SessionLocal() as session:
        user = await session.get(User, current_user.id)
        manager_id = None if user.role == "boss" else user.id
        if preview or not confirmed:
            statement = select(func.count(Client.id))
            if manager_id is not None:
                statement = statement.where(Client.assigned_manager_id == manager_id)
            count = (await session.execute(statement)).scalar_one()
            if preview:
                return JSONResponse({"count": count}, headers={"Cache-Control": "no-store"})
            response = render(request, "export_confirm.html", {
                "current_user": user, "count": count,
            })
            response.headers["Cache-Control"] = "no-store"
            return response
        file_bytes = await export_clients_to_workbook(session=session, manager_id=manager_id)
    return StreamingResponse(
        iter([file_bytes]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="tasks_export.xlsx"'},
    )


@app.get("/import/template")
async def download_import_template(current_user: User = Depends(get_boss_user)) -> StreamingResponse:
    _ = current_user
    file_bytes = build_import_template()
    return StreamingResponse(
        iter([file_bytes]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="tasks_import_template.xlsx"'},
    )


app.include_router(imports_router)  # /import/template must precede /import/{batch_id}.


@app.post("/backup")
async def backup_now(current_user: User = Depends(get_boss_user)) -> RedirectResponse:
    _ = current_user
    create_backup()
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/health")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
