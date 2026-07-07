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
from app.models.user import User

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
) -> None:
    """Generate a magic link and email it.

    Always behaves the same regardless of whether the email is allowed —
    avoids leaking which addresses are on the allowlist. The allowlist
    is now the `users` table (seeded from `.env` at startup); the admin
    UI can add/remove without restarts.
    """
    email = email.strip().lower()

    user = await _email_is_active(db, email)
    if user is None:
        log.info("auth.request_login.rejected", email=email, reason="not_active_user")
        return

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
