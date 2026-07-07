"""FastAPI application entry point.

Wires up:
- Sentry (if configured)
- Structured logging
- Database lifespan
- Auth, health, home routes
- Static files
- Exception handler that converts auth failures to redirects
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import sentry_sdk
import structlog
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

from app.admin.routes import router as admin_router
from app.auth.bootstrap import bootstrap_users_from_env
from app.auth.deps import _AuthRedirect, auth_redirect_handler
from app.auth.routes import router as auth_router
from app.config import get_settings
from app.database import dispose_engine, get_sessionmaker
from app.logging import configure_logging
from app.printers.routes import router as printers_router
from app.printers.service import mark_offline_if_stale
from app.routes.health import router as health_router
from app.submissions.routes import router as submissions_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings)
    log = structlog.get_logger(__name__)

    if settings.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.env,
            traces_sample_rate=0.0,
            integrations=[
                StarletteIntegration(transaction_style="endpoint"),
                FastApiIntegration(transaction_style="endpoint"),
            ],
        )
        log.info("sentry.initialized", env=settings.env)
    else:
        log.info("sentry.skipped", reason="no_dsn")

    log.info(
        "app.startup",
        env=settings.env,
        site_url=settings.site_url,
        allowed_count=len(settings.allowed_email_set),
        operator_count=len(settings.operator_email_set),
    )

    # Seed users from .env so we don't lock ourselves out after a DB restore.
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            await bootstrap_users_from_env(session, settings)
    except Exception as e:
        log.error("bootstrap.failed", error=str(e))

    # Background task: every few seconds, flip printers to OFFLINE if the
    # bridge has stopped reporting. Lets the widget show "offline since" even
    # when no fresh telemetry is coming in.
    async def _stale_loop():
        import asyncio

        sm = get_sessionmaker()
        try:
            while True:
                await asyncio.sleep(max(5, settings.printer_stale_seconds // 2))
                try:
                    async with sm() as session:
                        await mark_offline_if_stale(
                            session, stale_after_seconds=settings.printer_stale_seconds
                        )
                except Exception as e:
                    log.warning("stale_loop.error", error=str(e))
        except asyncio.CancelledError:
            return

    import asyncio

    stale_task = asyncio.create_task(_stale_loop())

    yield

    stale_task.cancel()
    try:
        await stale_task
    except asyncio.CancelledError:
        pass
    log.info("app.shutdown")
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.site_name,
        lifespan=lifespan,
        # Hide docs in production. They're handy in dev.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    app.add_exception_handler(_AuthRedirect, auth_redirect_handler)

    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(submissions_router)
    app.include_router(printers_router)
    app.include_router(admin_router)

    return app


app = create_app()
