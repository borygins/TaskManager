from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import urlencode

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.api.deps import get_boss_user, get_current_user
from app.core.config import settings
from app.db.session import SessionLocal, init_db
from app.models import User
from app.services.auth import authenticate_user
from app.services.backup import create_backup
from app.services.excel import build_import_template, export_clients_to_workbook, import_clients_from_excel
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

    if not scheduler.running:
        scheduler.add_job(
            create_backup,
            CronTrigger(hour=settings.backup_hour, minute=0),
            id="daily-backup",
            replace_existing=True,
        )
        scheduler.start()

    yield

    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 12)
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")


def render(request: Request, template_name: str, context: dict) -> HTMLResponse:
    base_context = {
        "request": request,
        "statuses": settings.default_statuses,
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
    employee_id = str(form.get("employee_id", "")).strip()
    password = str(form.get("password", ""))
    async with SessionLocal() as session:
        user = await authenticate_user(employee_id=employee_id, password=password, session=session)
    if not user:
        return render(request, "login.html", {"error": "Неверный табельный номер или пароль."})
    request.session["user_id"] = user.id
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


@app.get("/funnels/{funnel_id}", response_class=HTMLResponse)
async def funnel_detail(
    request: Request,
    funnel_id: int,
    inn: str | None = None,
    status_filter: str | None = None,
    next_call_date_filter: str | None = None,
    current_user: User = Depends(get_current_user),
) -> HTMLResponse:
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
            inn_query=inn,
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
            "search_inn": inn or "",
            "selected_status": status_filter or "",
            "selected_next_call_date": next_call_date_filter or "",
        },
    )


@app.get("/clients/{client_id}", response_class=HTMLResponse)
async def client_detail_page(
    request: Request,
    client_id: int,
    funnel_id: int | None = None,
    inn: str | None = None,
    status_filter: str | None = None,
    next_call_date_filter: str | None = None,
    current_user: User = Depends(get_current_user),
) -> HTMLResponse:
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
            "search_inn": inn or "",
            "selected_status": status_filter or "",
            "selected_next_call_date": next_call_date_filter or "",
        },
    )


@app.get("/boss/distribution", response_class=HTMLResponse)
async def boss_distribution(
    request: Request,
    inn: str | None = None,
    status_filter: str | None = None,
    manager_id: str | None = None,
    current_user: User = Depends(get_boss_user),
) -> HTMLResponse:
    manager_id_value = int(manager_id) if manager_id and manager_id.isdigit() else None
    async with SessionLocal() as session:
        user = await session.get(User, current_user.id)
        managers = await get_managers(session)
        clients = await get_clients_for_distribution(
            session=session,
            inn_query=inn,
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
            "search_inn": inn or "",
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
    inn = str(form.get("search_inn", "")).strip()
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
            inn_query=inn,
            status_filter=status_filter,
            manager_id=manager_filter,
        )
        if new_manager:
            await bulk_assign_clients(clients=clients, new_manager=new_manager, actor=user, session=session)
            await session.commit()

    query = {
        key: value
        for key, value in {
            "inn": inn,
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
            "inn": str(form.get("search_inn", "")).strip(),
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
        await update_client_task(client=client, form_data=dict(form), actor=user, session=session)
        await session.commit()

    funnel_id_raw = str(form.get("funnel_id", "")).strip()
    query = {
        key: value
        for key, value in {
            "inn": str(form.get("search_inn", "")).strip(),
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
async def export_excel(current_user: User = Depends(get_current_user)) -> StreamingResponse:
    async with SessionLocal() as session:
        user = await session.get(User, current_user.id)
        manager_id = None if user.role == "boss" else user.id
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


@app.post("/import")
async def import_excel(
    file: UploadFile = File(...),
    current_user: User = Depends(get_boss_user),
) -> RedirectResponse:
    _ = current_user
    file_bytes = await file.read()
    async with SessionLocal() as session:
        await import_clients_from_excel(file_bytes=file_bytes, session=session)
        await session.commit()
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/backup")
async def backup_now(current_user: User = Depends(get_boss_user)) -> RedirectResponse:
    _ = current_user
    create_backup()
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/health")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
