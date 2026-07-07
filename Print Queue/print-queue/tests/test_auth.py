"""Auth tests.

These cover:
- allowlist enforcement (disallowed emails don't get a magic link)
- magic-link request → DB row inserted
- verifying a valid token sets a session cookie and creates the user
- expired / used / unknown tokens fail
- protected routes redirect unauthenticated users to /login
- operator vs submitter role assignment from env
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.auth.service import (
    SESSION_COOKIE_NAME,
    AuthError,
    create_session_cookie,
    request_login,
    verify_token,
)
from app.auth.tokens import generate_token, hash_token
from app.config import get_settings
from app.models.magic_link import MagicLink
from app.models.user import User, UserRole


@pytest.mark.asyncio
async def test_root_redirects_when_logged_out(client):
    r = await client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_login_page_renders(client):
    r = await client.get("/login")
    assert r.status_code == 200
    assert "Sign in" in r.text


@pytest.mark.asyncio
async def test_request_login_for_allowed_email_creates_magic_link(db_session):
    settings = get_settings()
    db_session.add(User(email="alice@example.com", role=UserRole.operator, is_active=True))
    await db_session.commit()

    await request_login(db_session, settings, "alice@example.com")

    result = await db_session.execute(select(MagicLink))
    links = result.scalars().all()
    assert len(links) == 1
    assert links[0].email == "alice@example.com"
    assert links[0].used_at is None
    expires_at = links[0].expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    assert expires_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_request_login_for_inactive_user_creates_nothing(db_session):
    """A user that exists but is deactivated does not receive a magic link."""
    settings = get_settings()
    db_session.add(User(email="alice@example.com", role=UserRole.operator, is_active=False))
    await db_session.commit()

    await request_login(db_session, settings, "alice@example.com")
    result = await db_session.execute(select(MagicLink))
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_request_login_for_disallowed_email_creates_nothing(db_session):
    # Email with no User row at all → no magic link.
    settings = get_settings()
    await request_login(db_session, settings, "intruder@example.com")

    result = await db_session.execute(select(MagicLink))
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_request_login_normalizes_case(db_session):
    settings = get_settings()
    db_session.add(User(email="alice@example.com", role=UserRole.operator, is_active=True))
    await db_session.commit()

    await request_login(db_session, settings, "Alice@EXAMPLE.com")
    result = await db_session.execute(select(MagicLink))
    links = result.scalars().all()
    assert len(links) == 1
    assert links[0].email == "alice@example.com"


@pytest.mark.asyncio
async def test_verify_token_consumes_link_for_active_user(db_session):
    settings = get_settings()
    db_session.add(User(email="alice@example.com", role=UserRole.operator, is_active=True))
    await db_session.commit()

    raw = generate_token()
    db_session.add(
        MagicLink(
            email="alice@example.com",
            token_hash=hash_token(raw),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    await db_session.commit()

    user = await verify_token(db_session, settings, raw)

    assert user.email == "alice@example.com"
    assert user.role == UserRole.operator  # role comes from the DB row
    assert user.last_login_at is not None

    # Link is consumed.
    result = await db_session.execute(select(MagicLink))
    link = result.scalar_one()
    assert link.used_at is not None


@pytest.mark.asyncio
async def test_verify_token_rejects_deactivated_user(db_session):
    """A valid token for a now-deactivated user is rejected."""
    settings = get_settings()
    db_session.add(User(email="alice@example.com", role=UserRole.operator, is_active=False))
    await db_session.commit()

    raw = generate_token()
    db_session.add(
        MagicLink(
            email="alice@example.com",
            token_hash=hash_token(raw),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    await db_session.commit()

    with pytest.raises(AuthError, match="email_no_longer_allowed"):
        await verify_token(db_session, settings, raw)


@pytest.mark.asyncio
async def test_verify_token_keeps_db_role(db_session):
    settings = get_settings()
    db_session.add(User(email="bob@example.com", role=UserRole.submitter, is_active=True))
    await db_session.commit()

    raw = generate_token()
    db_session.add(
        MagicLink(
            email="bob@example.com",
            token_hash=hash_token(raw),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    await db_session.commit()

    user = await verify_token(db_session, settings, raw)
    assert user.role == UserRole.submitter


@pytest.mark.asyncio
async def test_verify_token_rejects_unknown(db_session):
    settings = get_settings()
    with pytest.raises(AuthError, match="token_unknown"):
        await verify_token(db_session, settings, "not-a-real-token")


@pytest.mark.asyncio
async def test_verify_token_rejects_expired(db_session):
    settings = get_settings()
    raw = generate_token()
    db_session.add(
        MagicLink(
            email="alice@example.com",
            token_hash=hash_token(raw),
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await db_session.commit()

    with pytest.raises(AuthError, match="token_expired"):
        await verify_token(db_session, settings, raw)


@pytest.mark.asyncio
async def test_verify_token_rejects_reuse(db_session):
    settings = get_settings()
    raw = generate_token()
    db_session.add(
        MagicLink(
            email="alice@example.com",
            token_hash=hash_token(raw),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            used_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    with pytest.raises(AuthError, match="token_already_used"):
        await verify_token(db_session, settings, raw)


@pytest.mark.asyncio
async def test_login_submit_returns_sent_page_for_any_email(client):
    # Allowlisted email
    r = await client.post("/auth/login", data={"email": "alice@example.com"})
    assert r.status_code == 200
    assert "Check your email" in r.text

    # Non-allowlisted: same response, no enumeration.
    r = await client.post("/auth/login", data={"email": "intruder@example.com"})
    assert r.status_code == 200
    assert "Check your email" in r.text


@pytest.mark.asyncio
async def test_full_login_flow_via_http(client, db_session):
    # User must exist as an active row (bootstrap normally seeds this).
    db_session.add(User(email="alice@example.com", role=UserRole.operator, is_active=True))
    await db_session.commit()

    # Request login
    r = await client.post("/auth/login", data={"email": "alice@example.com"})
    assert r.status_code == 200

    # Grab the token from DB (the email goes to console in tests)
    result = await db_session.execute(select(MagicLink))
    link = result.scalar_one()
    # We can't recover the raw token (only its hash is stored), so we
    # forge one for this test by stuffing a fresh link with a known raw.
    raw = generate_token()
    link.token_hash = hash_token(raw)
    await db_session.commit()

    # Verify
    r = await client.get(f"/auth/verify?token={raw}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert SESSION_COOKIE_NAME in r.cookies

    # Hit home with the cookie set
    cookie = r.cookies[SESSION_COOKIE_NAME]
    r = await client.get("/", cookies={SESSION_COOKIE_NAME: cookie})
    assert r.status_code == 200
    assert "alice@example.com" in r.text
    assert "operator" in r.text


@pytest.mark.asyncio
async def test_logout_clears_session(client, db_session):
    settings = get_settings()

    # Manually log in by setting a valid cookie
    user = User(email="alice@example.com", role=UserRole.operator)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    cookie_value = create_session_cookie(settings, user.id)

    r = await client.post(
        "/auth/logout",
        cookies={SESSION_COOKIE_NAME: cookie_value},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_invalid_verify_token_shows_error_page(client):
    r = await client.get("/auth/verify?token=garbage")
    assert r.status_code == 400
    assert "invalid or has expired" in r.text
