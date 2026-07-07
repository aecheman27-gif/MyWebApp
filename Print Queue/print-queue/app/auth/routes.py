"""Auth HTTP routes.

GET  /login              -> show the login form
POST /auth/login         -> submit email, send magic link
GET  /auth/verify        -> consume token, set session cookie
POST /auth/logout        -> clear cookie
"""

from __future__ import annotations

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import (
    SESSION_COOKIE_NAME,
    AuthError,
    create_session_cookie,
    request_login,
    verify_token,
)
from app.config import Settings, get_settings
from app.database import get_db

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/login")
async def login_form(request: Request, settings: Settings = Depends(get_settings)):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"site_name": settings.site_name},
    )


@router.post("/auth/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    # Best-effort validation. We don't return errors to the user about whether
    # the email is on the allowlist — see request_login() for rationale.
    try:
        validated = validate_email(email, check_deliverability=False)
        email_normalized = validated.normalized
    except EmailNotValidError:
        # Render the same "sent" page anyway, to avoid email enumeration.
        return templates.TemplateResponse(
            request,
            "login_sent.html",
            {"site_name": settings.site_name, "email": email},
        )

    await request_login(db, settings, email_normalized)
    return templates.TemplateResponse(
        request,
        "login_sent.html",
        {"site_name": settings.site_name, "email": email_normalized},
    )


@router.get("/auth/verify")
async def verify(
    request: Request,
    token: str,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    try:
        user = await verify_token(db, settings, token)
    except AuthError:
        # Generic error page — never leak why the token failed.
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "site_name": settings.site_name,
                "error": "That link is invalid or has expired. Please request a new one.",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    cookie_value = create_session_cookie(settings, user.id)
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=cookie_value,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        max_age=settings.session_ttl_days * 24 * 3600,
        path="/",
    )
    return response


@router.post("/auth/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response
