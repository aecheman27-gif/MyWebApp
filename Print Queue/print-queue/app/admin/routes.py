"""Operator-only admin routes:
- GET  /admin/users                       — list + add form
- POST /admin/users                       — create or reactivate a user
- POST /admin/users/{id}/role             — set role
- POST /admin/users/{id}/active           — activate / deactivate
- GET  /admin/stats                       — dashboard
- GET  /admin/stats/export.csv            — CSV export
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import stats_service, users_service
from app.auth.deps import require_operator
from app.config import Settings, get_settings
from app.database import get_db
from app.models.user import User, UserRole

router = APIRouter(prefix="/admin", tags=["admin"])
log = structlog.get_logger(__name__)
templates = Jinja2Templates(directory="app/templates")


@router.get("/users", response_class=HTMLResponse)
async def admin_users_page(
    request: Request,
    user: User = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    users = await users_service.list_users(db)
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {
            "site_name": settings.site_name,
            "user": user,
            "users": users,
            "roles": list(UserRole),
        },
    )


@router.post("/users")
async def admin_users_add(
    email: str = Form(...),
    role: str = Form(default="submitter"),
    user: User = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    try:
        role_enum = UserRole(role)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid role") from e
    try:
        await users_service.add_user(db, email=email, role=role_enum, actor=user)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return RedirectResponse("/admin/users", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{user_id}/role")
async def admin_user_role(
    user_id: UUID,
    role: str = Form(...),
    user: User = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    try:
        role_enum = UserRole(role)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid role") from e
    try:
        await users_service.set_role(db, user_id=user_id, role=role_enum, actor=user)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return RedirectResponse("/admin/users", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{user_id}/active")
async def admin_user_active(
    user_id: UUID,
    is_active: str = Form(...),
    user: User = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    flag = is_active.lower() in ("1", "true", "yes", "on")
    try:
        await users_service.set_active(db, user_id=user_id, is_active=flag, actor=user)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return RedirectResponse("/admin/users", status_code=status.HTTP_303_SEE_OTHER)


def _parse_range(start_str: str | None, end_str: str | None) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    if start_str:
        try:
            start = datetime.fromisoformat(start_str).astimezone(UTC)
        except ValueError:
            start = now - timedelta(days=30)
    else:
        start = now - timedelta(days=30)
    if end_str:
        try:
            end = datetime.fromisoformat(end_str).astimezone(UTC)
        except ValueError:
            end = now
    else:
        end = now
    return start, end


@router.get("/stats", response_class=HTMLResponse)
async def admin_stats_page(
    request: Request,
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    user: User = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    range_start, range_end = _parse_range(start, end)
    stats = await stats_service.compute_stats(db, range_start=range_start, range_end=range_end)
    return templates.TemplateResponse(
        request,
        "admin_stats.html",
        {
            "site_name": settings.site_name,
            "user": user,
            "stats": stats,
            "format_minutes": stats_service.format_minutes,
            "start_iso": range_start.date().isoformat(),
            "end_iso": range_end.date().isoformat(),
        },
    )


@router.get("/stats/export.csv")
async def admin_stats_export(
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    user: User = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    range_start, range_end = _parse_range(start, end)
    csv_text = await stats_service.csv_export_submissions(db, range_start, range_end)
    filename = f"submissions_{range_start.date()}_to_{range_end.date()}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
