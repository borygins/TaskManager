from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.api.contacts import check_csrf, render
from app.api.deps import get_current_user
from app.db.session import SessionLocal
from app.models import Client, Funnel, User
from app.services.clients import ClientAlreadyExists, create_client, parse_new_client, search_clients


router = APIRouter()


@router.get("/clients/search", response_class=HTMLResponse)
async def client_search(request: Request, q: str = Query("", max_length=255),
                        status: str = Query("", max_length=64),
                        funnel_id: str = Query("", pattern=r"^(?:|[1-9][0-9]{0,17})$"),
                        owner: str = Query("", pattern=r"^(?:|mine|unassigned|[1-9][0-9]{0,17})$"),
                        page: int = Query(1, ge=1), user: User = Depends(get_current_user)):
    search_submitted = any(name in request.query_params for name in ("q", "status", "funnel_id", "owner"))
    has_filters = bool(q.strip() or status or funnel_id or owner)
    async with SessionLocal() as session:
        if has_filters:
            clients, total, page, pages = await search_clients(
                session, user, q, status, int(funnel_id) if funnel_id else None, owner, page,
            )
        else:
            clients, total, page, pages = [], 0, 1, 1
        funnels = list((await session.scalars(select(Funnel).order_by(Funnel.name))).all())
        managers = list((await session.scalars(select(User).where(or_(
            User.role == "manager", User.clients.any(),
        )).order_by(User.full_name, User.id))).all())
        statuses = list((await session.scalars(select(Client.status).distinct().order_by(Client.status))).all())
    filters = {"q": q, "status": status, "funnel_id": funnel_id, "owner": owner}
    return render(request, "client_search.html", {
        "current_user": user, "clients": clients, "total": total, "page": page, "pages": pages,
        "filters": filters, "funnels": funnels, "managers": managers, "statuses": statuses,
        "has_filters": has_filters, "search_submitted": search_submitted,
        "previous_url": "/clients/search?" + urlencode({**filters, "page": page - 1}) if page > 1 else None,
        "next_url": "/clients/search?" + urlencode({**filters, "page": page + 1}) if page < pages else None,
    })


async def new_client_response(request: Request, user: User, values=None, error=None, status_code=200):
    async with SessionLocal() as session:
        funnels = list((await session.execute(select(Funnel).order_by(Funnel.name))).scalars())
    if values is None:
        selected = request.query_params.get("funnel_id", "")
        if selected not in {str(funnel.id) for funnel in funnels}:
            selected = ""
        values = {"tax_number": "", "company_name": "", "full_name": "", "phone": "", "funnel_id": selected}
    return render(request, "client_new.html", {
        "current_user": user, "funnels": funnels, "values": values, "error": str(error) if error else None,
        "existing_client_id": error.accessible_client_id if isinstance(error, ClientAlreadyExists) else None,
    }, status_code)


@router.get("/clients/new", response_class=HTMLResponse)
async def new_client_form(request: Request, user: User = Depends(get_current_user)):
    return await new_client_response(request, user)


@router.post("/clients/new")
async def new_client_action(request: Request, user: User = Depends(get_current_user)):
    form = {key: str(value) for key, value in (await request.form()).items()}
    check_csrf(request, form)
    try:
        data = parse_new_client(form)
        async with SessionLocal() as session:
            client = await create_client(session, user, data)
            client_id = client.id
            await session.commit()
    except ClientAlreadyExists as exc:
        return await new_client_response(request, user, form, exc, 409)
    except ValueError as exc:
        return await new_client_response(request, user, form, exc, 400)
    except IntegrityError:
        # The unique INN constraint also protects simultaneous submissions.
        async with SessionLocal() as session:
            existing = (await session.execute(select(Client).where(Client.tax_number == data.tax_number))).scalar_one_or_none()
            if existing is None:
                raise
            error = ClientAlreadyExists(existing, user)
        return await new_client_response(request, user, form, error, 409)
    return RedirectResponse(f"/clients/{client_id}?funnel_id={data.funnel_id}&created=1", status_code=303)
