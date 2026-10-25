from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import text

from app.api.contacts import check_csrf, render
from app.api.deps import get_boss_user
from app.db.session import SessionLocal
from app.models import User
from app.models.entities import ClientImport
from app.services.client_import import IMPORT_POLICY_VERSION, MAX_FILE_BYTES, apply_plan, build_plan, report_workbook, fingerprint, parse_workbook

router = APIRouter()


async def owned_batch(session, batch_id: str, user: User) -> ClientImport:
    batch = await session.get(ClientImport, batch_id)
    if batch is None:
        raise HTTPException(404, "Загрузка не найдена.")
    if batch.author_id != user.id:
        raise HTTPException(403, "Эта загрузка принадлежит другому пользователю.")
    return batch


@router.get("/import")
async def import_page(request: Request, user: User = Depends(get_boss_user)):
    return render(request, "client_import.html", {"current_user": user, "batch": None})


@router.post("/import")
async def preview_import(request: Request, file: UploadFile = File(...), user: User = Depends(get_boss_user)):
    form = await request.form()
    check_csrf(request, {"csrf_token": str(form.get("csrf_token", ""))})
    try:
        content = await file.read(MAX_FILE_BYTES + 1)
        source = await asyncio.to_thread(parse_workbook, content)
    except ValueError as exc:
        return render(request, "client_import.html", {
            "current_user": user, "batch": None, "error": str(exc),
        }, 400)
    finally:
        await file.close()
    async with SessionLocal() as session:
        plan = await build_plan(session, source)
        batch = ClientImport(id=uuid4().hex, author_id=user.id,
                             filename=(file.filename or "clients.xlsx")[:255],
                             payload=json.dumps({"source": source, "plan": plan}, ensure_ascii=False))
        session.add(batch)
        await session.commit()
    return RedirectResponse(f"/import/{batch.id}", status_code=303)


@router.get("/import/{batch_id}/errors", include_in_schema=False)
@router.get("/import/{batch_id}/report")
async def import_report(batch_id: str, user: User = Depends(get_boss_user)):
    async with SessionLocal() as session:
        batch = await owned_batch(session, batch_id, user)
        data = json.loads(batch.payload)
        state = batch.state
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if state == "pending" and (data["plan"].get("version") != IMPORT_POLICY_VERSION
                                  or batch.created_at < now - timedelta(hours=24)):
            state = "stale"
    content = await asyncio.to_thread(report_workbook, data["source"], data["plan"], state)
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={
        "Content-Disposition": 'attachment; filename="import_report.xlsx"', "Cache-Control": "no-store",
    })


@router.get("/import/{batch_id}")
async def import_result(request: Request, batch_id: str, user: User = Depends(get_boss_user)):
    async with SessionLocal() as session:
        batch = await owned_batch(session, batch_id, user)
        plan = json.loads(batch.payload)["plan"]
    response = render(request, "client_import.html", {
        "current_user": user, "batch": batch, "plan": plan,
        "outdated_policy": batch.state == "pending" and plan.get("version") != IMPORT_POLICY_VERSION,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/import/{batch_id}/confirm")
async def confirm_import(request: Request, batch_id: str, user: User = Depends(get_boss_user)):
    form = await request.form()
    check_csrf(request, {"csrf_token": str(form.get("csrf_token", ""))})
    async with SessionLocal() as session:
        # Reserve the SQLite writer before reading snapshots: check + apply + receipt are atomic.
        await session.execute(text("BEGIN IMMEDIATE"))
        batch = await owned_batch(session, batch_id, user)
        if batch.state == "pending":
            data = json.loads(batch.payload)
            current = await build_plan(session, data["source"])
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if batch.created_at < now - timedelta(hours=24) or fingerprint(current) != fingerprint(data["plan"]):
                batch.state = "stale"
            else:
                await apply_plan(session, user, current)
                batch.state = "applied"
                batch.applied_at = now
            await session.commit()
        # Applied, cancelled and stale batches are never executed again.
    return RedirectResponse(f"/import/{batch.id}", status_code=303)


@router.post("/import/{batch_id}/cancel")
async def cancel_import(request: Request, batch_id: str, user: User = Depends(get_boss_user)):
    form = await request.form()
    check_csrf(request, {"csrf_token": str(form.get("csrf_token", ""))})
    async with SessionLocal() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        batch = await owned_batch(session, batch_id, user)
        if batch.state == "pending":
            batch.state = "cancelled"
            await session.commit()
    return RedirectResponse(f"/import/{batch.id}", status_code=303)
