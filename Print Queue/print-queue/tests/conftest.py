"""Test fixtures.

Tests run against an in-memory SQLite DB and a tmp-dir LocalFileStorage.
Both are fast and need no external services.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Test env BEFORE importing the app — config is read at import time.
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["SESSION_SECRET"] = "test-secret-with-enough-length-yes-it-is-long-enough"
os.environ["ALLOWED_EMAILS"] = "alice@example.com,bob@example.com,carol@example.com"
os.environ["OPERATOR_EMAILS"] = "alice@example.com"
os.environ["RESEND_API_KEY"] = ""
os.environ["SITE_URL"] = "http://testserver"
os.environ["ENV"] = "development"
os.environ["MAX_UPLOAD_MB"] = "10"

# Each test run gets its own tmp upload dir.
_TMP_UPLOAD = Path(tempfile.mkdtemp(prefix="printq-test-uploads-"))
os.environ["UPLOAD_DIR"] = str(_TMP_UPLOAD)

from app import database as db_module  # noqa: E402
from app.auth.service import SESSION_COOKIE_NAME, create_session_cookie  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402
from app.storage.factory import reset_storage_cache  # noqa: E402


@pytest_asyncio.fixture
async def engine():
    get_settings.cache_clear()
    reset_storage_cache()
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture
async def db_session(session_factory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


@pytest_asyncio.fixture
async def client(engine, session_factory) -> AsyncIterator[AsyncClient]:
    db_module._engine = engine
    db_module._sessionmaker = session_factory

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c

    db_module._engine = None
    db_module._sessionmaker = None


@pytest.fixture
def settings():
    get_settings.cache_clear()
    return get_settings()


@pytest_asyncio.fixture
async def operator(db_session) -> User:
    """An operator user (alice)."""
    u = User(email="alice@example.com", role=UserRole.operator)
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


@pytest_asyncio.fixture
async def submitter(db_session) -> User:
    """A submitter user (bob)."""
    u = User(email="bob@example.com", role=UserRole.submitter)
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


@pytest_asyncio.fixture
async def other_submitter(db_session) -> User:
    """A second submitter (carol) — used for permission tests."""
    u = User(email="carol@example.com", role=UserRole.submitter)
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


@pytest.fixture
def operator_cookies(operator) -> dict[str, str]:
    cookie = create_session_cookie(get_settings(), operator.id)
    return {SESSION_COOKIE_NAME: cookie}


@pytest.fixture
def submitter_cookies(submitter) -> dict[str, str]:
    cookie = create_session_cookie(get_settings(), submitter.id)
    return {SESSION_COOKIE_NAME: cookie}
