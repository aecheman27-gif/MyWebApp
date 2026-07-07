"""FastAPI dependencies for authentication and authorization.

Usage in routes:

    @router.get("/")
    async def home(user: User = Depends(get_current_user)):
        ...

    @router.delete("/things/{id}")
    async def delete(user: User = Depends(require_operator)):
        ...
"""

from __future__ import annotations

from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import SESSION_COOKIE_NAME, read_session_cookie
from app.config import Settings, get_settings
from app.database import get_db
from app.models.user import User, UserRole


class _AuthRedirect(HTTPException):
    """HTTPException that the global handler turns into a redirect to /login."""


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> User:
    if session is None:
        raise _AuthRedirect(status_code=status.HTTP_307_TEMPORARY_REDIRECT, detail="login_required")
    user_id = read_session_cookie(settings, session)
    if user_id is None:
        raise _AuthRedirect(status_code=status.HTTP_307_TEMPORARY_REDIRECT, detail="login_required")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise _AuthRedirect(status_code=status.HTTP_307_TEMPORARY_REDIRECT, detail="login_required")

    # Enforce that the user is still active. Operator deactivates via admin
    # UI -> User.is_active=False -> their next request gets redirected.
    if not user.is_active:
        raise _AuthRedirect(status_code=status.HTTP_307_TEMPORARY_REDIRECT, detail="not_allowed")

    return user


async def get_optional_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> User | None:
    """Like get_current_user but returns None instead of redirecting."""
    if session is None:
        return None
    user_id = read_session_cookie(settings, session)
    if user_id is None:
        return None
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    return user


async def require_operator(
    user: User = Depends(get_current_user),
) -> User:
    if user.role != UserRole.operator:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="operator role required")
    return user


def auth_redirect_handler(request: Request, exc: _AuthRedirect) -> RedirectResponse:
    """Convert auth failures into a redirect to /login."""
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
