from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.contacts import check_csrf, render
from app.api.deps import get_current_user
from app.db.session import SessionLocal
from app.models import Client, Funnel, User
from app.services.clients import ClientAlreadyExists, create_client, parse_new_client


router = APIRouter()


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
