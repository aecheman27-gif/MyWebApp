#!/usr/bin/env bash
# Print-queue role-access overhaul installer (request->approve + submitter lockdown)
set -euo pipefail
if [ ! -f docker-compose.yml ]; then
  echo "ERROR: run this from your project root (cd ~/print-queue) — docker-compose.yml not found here."; exit 1
fi
echo "Writing updated files..."
mkdir -p app/auth app/submissions app/templates

cat > app/auth/service.py << '__EOF_app_auth_service_py__'
"""Auth business logic.

Two main operations:
    request_login(email)  -> creates a magic link, emails it
    verify_token(token)   -> consumes the link, returns the User

Session cookies are signed JWT-ish payloads using itsdangerous's
URLSafeTimedSerializer. The serializer handles both signing and TTL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import generate_token, hash_token
from app.config import Settings
from app.email.resend_client import send_magic_link_email
from app.models.magic_link import MagicLink
from app.models.user import User, UserRole

log = structlog.get_logger(__name__)

SESSION_COOKIE_NAME = "printq_session"
SESSION_SALT = "printq-session-v1"


def _aware_utc(dt: datetime | None) -> datetime | None:
    """Normalize a possibly-naive datetime to timezone-aware UTC.

    Postgres with `DateTime(timezone=True)` returns aware datetimes, but
    SQLite (used in tests) strips tzinfo on round-trip. This makes all
    downstream comparisons safe regardless of dialect.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


class AuthError(Exception):
    """Raised when an auth operation fails."""


def get_serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt=SESSION_SALT)


async def _email_is_active(db: AsyncSession, email: str) -> User | None:
    """Returns the User row if the email belongs to an active user, else None."""
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    return user


async def request_login(
    db: AsyncSession,
    settings: Settings,
    email: str,
) -> str:
    """Handle a sign-in / access request. Returns a status string.

    - "sent": the email belongs to an active user; a magic link was emailed.
    - "pending": a new access request (creates an inactive submitter that an
      operator must approve), or an existing not-yet-approved/deactivated
      account. No link is sent until an operator approves.
    """
    email = email.strip().lower()

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user is not None and user.is_active:
        raw_token = generate_token()
        link = MagicLink(
            email=email,
            token_hash=hash_token(raw_token),
            expires_at=datetime.now(UTC) + timedelta(minutes=settings.magic_link_ttl_minutes),
        )
        db.add(link)
        await db.commit()

        verify_url = f"{settings.site_url.rstrip('/')}/auth/verify?token={raw_token}"
        await send_magic_link_email(
            settings=settings,
            to_email=email,
            verify_url=verify_url,
        )
        log.info("auth.request_login.sent", email=email)
        return "sent"

    if user is None:
        # New access request -> create a pending (inactive) submitter that an
        # operator approves from the Users page.
        db.add(User(email=email, role=UserRole.submitter, is_active=False))
        await db.commit()
        log.info("auth.access_requested", email=email)
        return "pending"

    # Known but inactive: either awaiting approval or deactivated.
    log.info("auth.access_request_repeat", email=email)
    return "pending"


async def verify_token(
    db: AsyncSession,
    settings: Settings,
    token: str,
) -> User:
    """Consume a magic-link token, return the user.

    Raises AuthError on any failure (expired, used, unknown, deactivated).
    Callers should treat all failures as a generic 'link invalid or
    expired' to the user.
    """
    token_hash = hash_token(token)
    result = await db.execute(select(MagicLink).where(MagicLink.token_hash == token_hash))
    link = result.scalar_one_or_none()
    now = datetime.now(UTC)

    if link is None:
        raise AuthError("token_unknown")
    if _aware_utc(link.used_at) is not None:
        raise AuthError("token_already_used")
    if _aware_utc(link.expires_at) <= now:
        raise AuthError("token_expired")

    user = await _email_is_active(db, link.email)
    if user is None:
        raise AuthError("email_no_longer_allowed")

    # Consume the link
    link.used_at = now
    user.last_login_at = now

    await db.commit()
    await db.refresh(user)
    log.info("auth.verify_token.success", email=user.email, role=user.role.value)
    return user


def create_session_cookie(settings: Settings, user_id: UUID) -> str:
    """Return the signed session cookie value for a given user id."""
    return get_serializer(settings).dumps({"uid": str(user_id)})


def read_session_cookie(settings: Settings, cookie_value: str) -> UUID | None:
    """Return the user id from a cookie value, or None if invalid/expired."""
    max_age_seconds = settings.session_ttl_days * 24 * 3600
    try:
        payload = get_serializer(settings).loads(cookie_value, max_age=max_age_seconds)
    except SignatureExpired:
        log.info("auth.session.expired")
        return None
    except BadSignature:
        log.warning("auth.session.bad_signature")
        return None

    uid_str = payload.get("uid")
    if not isinstance(uid_str, str):
        return None
    try:
        return UUID(uid_str)
    except ValueError:
        return None
__EOF_app_auth_service_py__
echo "  wrote app/auth/service.py"

cat > app/auth/routes.py << '__EOF_app_auth_routes_py__'
"""Auth HTTP routes.

GET  /login              -> show the login form
POST /auth/login         -> submit email, send magic link
GET  /auth/verify        -> show a confirmation page (does NOT consume token)
POST /auth/verify        -> consume token, set session cookie
POST /auth/logout        -> clear cookie

Two-step verify: corporate email security (Safe Links, Proofpoint, etc.)
pre-fetches links in incoming mail to scan them, which would burn a
single-use magic-link token before the human clicks. So the emailed link
(GET) only renders a confirmation page; the token is consumed only when the
user clicks the "Sign in" button (POST). Scanners follow GETs but don't
submit forms, so the token survives the scan.
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
        # Invalid address: show the neutral request-received page (no leak).
        return templates.TemplateResponse(
            request,
            "request_received.html",
            {"site_name": settings.site_name, "email": email},
        )

    status_ = await request_login(db, settings, email_normalized)
    template = "login_sent.html" if status_ == "sent" else "request_received.html"
    return templates.TemplateResponse(
        request,
        template,
        {"site_name": settings.site_name, "email": email_normalized},
    )


@router.get("/auth/verify")
async def verify_confirm_page(
    request: Request,
    token: str,
    settings: Settings = Depends(get_settings),
):
    # IMPORTANT: do not touch the token here. This handler must be safe to
    # call repeatedly (email scanners pre-fetch it). Just render a page with
    # a button that POSTs the token to actually sign in.
    return templates.TemplateResponse(
        request,
        "verify_confirm.html",
        {"site_name": settings.site_name, "token": token},
    )


@router.post("/auth/verify")
async def verify_submit(
    request: Request,
    token: str = Form(...),
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
__EOF_app_auth_routes_py__
echo "  wrote app/auth/routes.py"

cat > app/auth/bootstrap.py << '__EOF_app_auth_bootstrap_py__'
"""Bootstrap users from `.env` into the database on startup.

The allowlist used to be enforced purely from `.env`. We now keep the
source-of-truth in the database so admins can add/remove users via the
admin UI. To avoid lockouts on a fresh deploy (or after a DB restore),
we still read `.env` once at startup and ensure every email listed there
exists as an active user with the right role.

Behavior:
- For each email in ALLOWED_EMAILS, create-or-update User to is_active=True.
- For each email in OPERATOR_EMAILS, set role to operator.
- Users NOT in `.env` are left alone (admin UI can deactivate them).

The `.env` is therefore a safety net, not the runtime allowlist. Operators
can add new users via the admin UI without restarting.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.user import User, UserRole

log = structlog.get_logger(__name__)


async def bootstrap_users_from_env(db: AsyncSession, settings: Settings) -> int:
    """Returns the number of users created or updated."""
    allowed = settings.allowed_email_set
    operators = settings.operator_email_set
    # Submitters now self-request and are approved in the UI, so ALLOWED_EMAILS
    # may be empty. We still always seed the hard-coded operators so admins can
    # never be locked out.
    seed = allowed | operators
    if not seed:
        log.info("bootstrap.no_seed_emails")
        return 0

    changes = 0
    for email in seed:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        expected_role = UserRole.operator if email in operators else UserRole.submitter

        if user is None:
            user = User(
                email=email,
                role=expected_role,
                is_active=True,
            )
            db.add(user)
            changes += 1
            log.info("bootstrap.user_created", email=email, role=expected_role.value)
        else:
            updated = False
            if not user.is_active:
                user.is_active = True
                updated = True
            if user.role != expected_role:
                user.role = expected_role
                updated = True
            if updated:
                changes += 1
                log.info(
                    "bootstrap.user_updated",
                    email=email,
                    role=expected_role.value,
                )

    if changes:
        await db.commit()
    log.info("bootstrap.complete", changes=changes, total_seed=len(seed))
    return changes
__EOF_app_auth_bootstrap_py__
echo "  wrote app/auth/bootstrap.py"

cat > app/submissions/routes.py << '__EOF_app_submissions_routes_py__'
"""Submission HTTP routes.

GET  /                              -> queue dashboard (default /)
GET  /submissions/new               -> creation form
POST /submissions                   -> create submission
GET  /submissions/{id}              -> detail
POST /submissions/{id}/edit         -> edit (own if QUEUED; operator any)
POST /submissions/{id}/delete       -> delete (same rule)
POST /submissions/{id}/status       -> operator status transition (HTMX swap)
GET  /submissions/{id}/download     -> stream the STEP file (auth scoped)
"""

from __future__ import annotations

from uuid import UUID

import structlog
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user, require_operator
from app.config import Settings, get_settings
from app.database import get_db
from app.models.submission import (
    SubmissionMaterial,
    SubmissionPriority,
    SubmissionStatus,
)
from app.models.user import User
from app.printers.service import list_printer_states
from app.storage import FileStorage, get_storage
from app.submissions import permissions, service
from app.submissions.schemas import StatusChange, SubmissionCreate, SubmissionEdit

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
log = structlog.get_logger(__name__)


def _storage_dep() -> FileStorage:
    return get_storage()


@router.get("/", response_class=HTMLResponse)
async def queue_view(
    request: Request,
    filter: str = Query(default="active"),
    search: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    # Submitters never see the queue — they only submit.
    if not permissions.is_operator(user):
        return RedirectResponse(url="/submissions/new", status_code=status.HTTP_303_SEE_OTHER)
    if filter not in ("mine", "active", "in_progress", "completed", "all"):
        filter = "active"
    submissions = await service.list_submissions(
        db,
        filter_=filter,  # type: ignore[arg-type]
        user=user,
        search=search,
    )
    printer_states = await list_printer_states(db)
    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "site_name": settings.site_name,
            "user": user,
            "submissions": submissions,
            "printer_states": printer_states,
            "active_filter": filter,
            "search": search or "",
            "is_operator": permissions.is_operator(user),
        },
    )


@router.get("/submissions/new", response_class=HTMLResponse)
async def submission_form(
    request: Request,
    submitted: bool = Query(default=False),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    return templates.TemplateResponse(
        request,
        "submission_new.html",
        {
            "site_name": settings.site_name,
            "user": user,
            "materials": list(SubmissionMaterial),
            "priorities": list(SubmissionPriority),
            "max_upload_mb": settings.max_upload_mb,
            "submitted": submitted,
            "is_operator": permissions.is_operator(user),
        },
    )


@router.post("/submissions")
async def create_submission_endpoint(
    request: Request,
    part_name: str = Form(...),
    description: str | None = Form(default=None),
    material: str = Form(default="PLA"),
    priority: str = Form(default="NORMAL"),
    notes: str | None = Form(default=None),
    file: UploadFile | None = File(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    storage: FileStorage = Depends(_storage_dep),
):
    try:
        data = SubmissionCreate(
            part_name=part_name,
            description=description,
            material=material,  # type: ignore[arg-type]
            priority=priority,  # type: ignore[arg-type]
            notes=notes,
        )
    except ValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    file_bytes: bytes | None = None
    file_name: str | None = None
    file_mime = "application/octet-stream"
    if file is not None and file.filename:
        file_bytes = await file.read()
        file_name = file.filename
        if file.content_type:
            file_mime = file.content_type

    submission = await service.create_submission(
        db,
        settings,
        storage,
        user,
        data,
        file_bytes,
        file_name,
        file_mime,
    )
    if permissions.is_operator(user):
        return RedirectResponse(
            url=f"/submissions/{submission.id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    # Submitters can't view detail pages; send them back to a clean form
    # with a success confirmation.
    return RedirectResponse(
        url="/submissions/new?submitted=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/submissions/{submission_id}", response_class=HTMLResponse)
async def submission_detail(
    request: Request,
    submission_id: UUID,
    user: User = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    submission = await service.get_submission(db, submission_id)
    if submission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "submission not found")

    from app.comments import service as comment_service

    comments = await comment_service.list_for_submission(db, submission.id)

    return templates.TemplateResponse(
        request,
        "submission_detail.html",
        {
            "site_name": settings.site_name,
            "user": user,
            "submission": submission,
            "comments": comments,
            "can_edit": permissions.can_edit_submission(user, submission),
            "can_delete": permissions.can_delete_submission(user, submission),
            "can_change_status": permissions.can_change_status(user),
            "can_download": permissions.can_download_file(user, submission),
            "all_statuses": list(SubmissionStatus),
            "materials": list(SubmissionMaterial),
            "priorities": list(SubmissionPriority),
        },
    )


@router.post("/submissions/{submission_id}/edit")
async def edit_submission_endpoint(
    submission_id: UUID,
    part_name: str = Form(...),
    description: str | None = Form(default=None),
    material: str = Form(default="PLA"),
    priority: str = Form(default="NORMAL"),
    notes: str | None = Form(default=None),
    user: User = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    submission = await service.get_submission(db, submission_id)
    if submission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "submission not found")
    if not permissions.can_edit_submission(user, submission):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not allowed to edit")

    try:
        data = SubmissionEdit(
            part_name=part_name,
            description=description,
            material=material,  # type: ignore[arg-type]
            priority=priority,  # type: ignore[arg-type]
            notes=notes,
        )
    except ValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    await service.edit_submission(db, submission, user, data)
    return RedirectResponse(
        url=f"/submissions/{submission_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/submissions/{submission_id}/delete")
async def delete_submission_endpoint(
    submission_id: UUID,
    user: User = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
    storage: FileStorage = Depends(_storage_dep),
):
    submission = await service.get_submission(db, submission_id)
    if submission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "submission not found")
    if not permissions.can_delete_submission(user, submission):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not allowed to delete")
    await service.delete_submission(db, storage, submission, user)
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/submissions/{submission_id}/status", response_class=HTMLResponse)
async def change_status_endpoint(
    request: Request,
    submission_id: UUID,
    to_status: str = Form(...),
    user: User = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not permissions.can_change_status(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "operator role required")
    submission = await service.get_submission(db, submission_id)
    if submission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "submission not found")

    try:
        change = StatusChange(to_status=to_status)  # type: ignore[arg-type]
    except ValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    await service.change_status(db, submission, user, change.to_status, settings=settings)

    # If the request is an HTMX swap, return just the row partial.
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request,
            "_submission_row.html",
            {
                "user": user,
                "submission": submission,
                "is_operator": permissions.is_operator(user),
                "all_statuses": list(SubmissionStatus),
            },
        )
    # Otherwise redirect back to the detail page.
    return RedirectResponse(
        url=f"/submissions/{submission_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/submissions/{submission_id}/download")
async def download_file(
    submission_id: UUID,
    user: User = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
    storage: FileStorage = Depends(_storage_dep),
):
    submission = await service.get_submission(db, submission_id)
    if submission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "submission not found")
    if not permissions.can_download_file(user, submission):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not allowed to download")
    if submission.file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no file attached")

    f = submission.file
    return StreamingResponse(
        storage.stream(f.storage_key),
        media_type=f.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{f.original_filename}"',
            "Content-Length": str(f.size_bytes),
        },
    )


@router.post("/submissions/{submission_id}/comments")
async def add_comment_endpoint(
    submission_id: UUID,
    body: str = Form(...),
    user: User = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Add a comment. Anyone who can view the submission can comment.

    Permission model: same as detail view — submitter sees own; operator
    sees any. We re-fetch and check via service.get_submission which
    enforces scoping. Operators may also need to comment on others' work,
    which the existing detail-view check already allows.
    """
    submission = await service.get_submission(db, submission_id)
    if submission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "submission not found")
    if not permissions.can_view(user, submission):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not allowed")

    from app.comments import service as comment_service

    try:
        await comment_service.create(
            db,
            settings,
            submission=submission,
            author=user,
            body=body,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    return RedirectResponse(
        f"/submissions/{submission.id}#comments",
        status_code=status.HTTP_303_SEE_OTHER,
    )
__EOF_app_submissions_routes_py__
echo "  wrote app/submissions/routes.py"

cat > app/templates/base.html << '__EOF_app_templates_base_html__'
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{% block title %}{{ site_name }}{% endblock %}</title>
  <link rel="stylesheet" href="/static/styles.css">
</head>
<body>
  <header class="site-header">
    <a href="/" class="site-title">{{ site_name }}</a>
    {% if user %}
      <nav class="site-nav">
        {% if user.role.value == 'operator' %}
        <a href="/" class="link-button">Queue</a>
        {% endif %}
        <a href="/submissions/new" class="link-button">+ New</a>
        {% if user.role.value == 'operator' %}
          <a href="/admin/stats" class="link-button">Stats</a>
          <a href="/admin/users" class="link-button">Users</a>
        {% endif %}
        <span class="user-email">{{ user.email }}</span>
        <span class="user-role role-{{ user.role.value }}">{{ user.role.value }}</span>
        <form method="post" action="/auth/logout" class="logout-form">
          <button type="submit" class="link-button">Sign out</button>
        </form>
      </nav>
    {% endif %}
  </header>

  <main class="site-main">
    {% block content %}{% endblock %}
  </main>
</body>
</html>
__EOF_app_templates_base_html__
echo "  wrote app/templates/base.html"

cat > app/templates/login_sent.html << '__EOF_app_templates_login_sent_html__'
{% extends "base.html" %}
{% block title %}Check your email — {{ site_name }}{% endblock %}
{% block content %}
<div class="card narrow">
  <h1>Check your email</h1>
  <p>
    A sign-in link is on its way to <strong>{{ email }}</strong>. The link
    expires in 15 minutes.
  </p>
  <p class="muted">
    Didn't get it? Check spam, then <a href="/login">try again</a>.
  </p>
</div>
{% endblock %}
__EOF_app_templates_login_sent_html__
echo "  wrote app/templates/login_sent.html"

cat > app/templates/request_received.html << '__EOF_app_templates_request_received_html__'
{% extends "base.html" %}
{% block title %}Request received — {{ site_name }}{% endblock %}
{% block content %}
<div class="card narrow">
  <h1>Request received</h1>
  <p>
    Thanks — your access request for <strong>{{ email }}</strong> has been
    received. An administrator will review it, and you'll be able to sign in
    once your account is approved.
  </p>
  <p class="muted">
    If you already have access, check your email for the sign-in link, or
    <a href="/login">try again</a>.
  </p>
</div>
{% endblock %}
__EOF_app_templates_request_received_html__
echo "  wrote app/templates/request_received.html"

cat > app/templates/submission_new.html << '__EOF_app_templates_submission_new_html__'
{% extends "base.html" %}
{% block title %}New submission — {{ site_name }}{% endblock %}
{% block content %}
<div class="card narrow-form">
  {% if submitted %}
  <div class="banner-success" style="background:#10391f;border:1px solid #1f7a44;color:#9ff0bf;padding:12px 14px;border-radius:8px;margin-bottom:16px;">
    ✓ Your submission was received and added to the queue. You can submit another below.
  </div>
  {% endif %}
  <h1>New print submission</h1>
  <p class="muted">
    Upload a STEP, STP, or STL file. Max {{ max_upload_mb }} MB. The operator
    will pick this up from the queue, slice, and print.
  </p>

  <form method="post" action="/submissions" enctype="multipart/form-data" class="stack">
    <label>
      Part name
      <input type="text" name="part_name" required maxlength="200" autofocus>
    </label>

    <label>
      Description
      <textarea name="description" rows="3" maxlength="5000" placeholder="What is this for? Any tolerances or fit considerations?"></textarea>
    </label>

    <div class="row">
      <label>
        Material
        <select name="material">
          {% for m in materials %}
            <option value="{{ m.value }}" {{ 'selected' if m.value == 'PLA' else '' }}>{{ m.value }}</option>
          {% endfor %}
        </select>
      </label>

      <label>
        Priority
        <select name="priority">
          {% for p in priorities %}
            <option value="{{ p.value }}" {{ 'selected' if p.value == 'NORMAL' else '' }}>{{ p.value }}</option>
          {% endfor %}
        </select>
      </label>
    </div>

    <label>
      Notes for the operator
      <textarea name="notes" rows="3" maxlength="5000" placeholder="Layer height, infill, color, quantity, anything else"></textarea>
    </label>

    <label>
      STEP / STL file
      <input type="file" name="file" accept=".step,.stp,.stl">
    </label>

    <div class="form-actions">
      <a href="/" class="link-button">Cancel</a>
      <button type="submit" class="primary">Submit to queue</button>
    </div>
  </form>
</div>
{% endblock %}
__EOF_app_templates_submission_new_html__
echo "  wrote app/templates/submission_new.html"

cat > app/templates/admin_users.html << '__EOF_app_templates_admin_users_html__'
{% extends "base.html" %}
{% block title %}Users — {{ site_name }}{% endblock %}
{% block content %}

<div class="page-header">
  <h1>User management</h1>
  <p class="muted">Add, remove, and change roles. Deactivating prevents sign-in but preserves submission history.</p>
</div>

<div class="card">
  <h2 class="card-title">Add a user</h2>
  <form method="post" action="/admin/users" class="add-user-form">
    <label>
      Email
      <input type="email" name="email" required placeholder="name@spacex.com" autocomplete="off">
    </label>
    <label>
      Role
      <select name="role">
        {% for r in roles %}
          <option value="{{ r.value }}">{{ r.value }}</option>
        {% endfor %}
      </select>
    </label>
    <button type="submit" class="primary">Add user</button>
  </form>
</div>

<table class="queue-table users-table">
  <thead>
    <tr>
      <th>Email</th>
      <th>Role</th>
      <th>Status</th>
      <th>Last login</th>
      <th></th>
    </tr>
  </thead>
  <tbody>
    {% for u in users %}
      <tr>
        <td data-label="Email">{{ u.email }}</td>
        <td data-label="Role">
          <form method="post" action="/admin/users/{{ u.id }}/role" class="inline-form">
            <select name="role" onchange="this.form.submit()" {% if u.id == user.id %}disabled title="Can't change your own role"{% endif %}>
              {% for r in roles %}
                <option value="{{ r.value }}" {{ 'selected' if r == u.role else '' }}>{{ r.value }}</option>
              {% endfor %}
            </select>
            <noscript><button type="submit">Save</button></noscript>
          </form>
        </td>
        <td data-label="Status">
          {% if u.is_active %}
            <span class="status-pill status-FINISHED">Active</span>
          {% else %}
            <span class="status-pill status-OFFLINE">Inactive</span>
          {% endif %}
        </td>
        <td data-label="Last login" class="muted">
          {{ u.last_login_at.strftime('%b %d, %H:%M') if u.last_login_at else '—' }}
        </td>
        <td data-label="" class="row-actions">
          {% if u.id != user.id %}
            <form method="post" action="/admin/users/{{ u.id }}/active" class="inline-form">
              <input type="hidden" name="is_active" value="{{ 'false' if u.is_active else 'true' }}">
              <button type="submit" class="link-button">
                {{ 'Deactivate' if u.is_active else 'Activate' }}
              </button>
            </form>
          {% else %}
            <span class="muted">(you)</span>
          {% endif %}
        </td>
      </tr>
    {% endfor %}
  </tbody>
</table>
{% endblock %}
__EOF_app_templates_admin_users_html__
echo "  wrote app/templates/admin_users.html"

echo ""
echo "All 9 files updated. Now rebuild the web container:"
echo "    docker compose up -d --build web"
