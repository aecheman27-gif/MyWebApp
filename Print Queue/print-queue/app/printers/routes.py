"""Printer routes:
- POST /internal/telemetry  — bridge posts here. Network-isolated (Docker only).
- GET  /printer/stream      — browser SSE feed, requires auth.
- GET  /admin/printers      — operator-only JSON dump of current state.
"""

from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user, require_operator
from app.config import Settings, get_settings
from app.database import get_db
from app.models.user import User
from app.printers import service
from app.printers.pubsub import broadcaster
from app.printers.schemas import TelemetryIn

router = APIRouter()
log = structlog.get_logger(__name__)


def _check_internal(request: Request, settings: Settings) -> bool:
    """The /internal/* routes are not exposed publicly because Cloudflare
    Tunnel only routes the configured public hostname. But we also want
    to reject any request that somehow leaks in — defense in depth.

    Accept the request only if it carries the shared bridge token. The
    bridge sets this in its environment; cloudflared/the public side
    never sees it.
    """
    if not settings.bridge_shared_token:
        return True  # token enforcement disabled (dev / first-time setup)
    presented = request.headers.get("X-Bridge-Token", "")
    return presented == settings.bridge_shared_token


@router.post("/internal/telemetry", status_code=204)
async def receive_telemetry(
    request: Request,
    telemetry: TelemetryIn,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    from fastapi import HTTPException, status

    if not _check_internal(request, settings):
        log.warning("telemetry.rejected", reason="bad_token")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    await service.ingest_telemetry(db, telemetry)
    return None


@router.get("/printer/stream")
async def printer_stream(
    user: User = Depends(get_current_user),
):
    """Server-Sent Events stream of telemetry updates.

    The widget on the queue page opens this with the browser's EventSource
    API; each new event is one SSE message containing JSON.
    """

    async def gen():
        # Send a hello so the connection is established cleanly even when
        # no telemetry has arrived yet.
        yield "event: hello\ndata: {}\n\n"
        try:
            async for line in broadcaster.subscribe():
                yield f"data: {line}\n\n"
        except asyncio.CancelledError:
            # Client disconnected; clean exit.
            return

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/admin/printers")
async def admin_printers(
    user: User = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    items = await service.list_printer_states(db)
    return [
        {
            "slug": p.slug,
            "name": p.name,
            "serial": p.serial,
            "location": p.location,
            "enabled": p.enabled,
            "state": (
                {
                    "status": s.status.value,
                    "last_seen_at": s.last_seen_at.isoformat() if s.last_seen_at else None,
                    "current_file": s.current_file,
                    "percent": s.percent,
                    "remaining_minutes": s.remaining_minutes,
                    "layer": s.layer,
                    "total_layers": s.total_layers,
                    "nozzle_temp": s.nozzle_temp,
                    "bed_temp": s.bed_temp,
                    "error_code": s.error_code,
                    "current_submission_id": (
                        str(s.current_submission_id) if s.current_submission_id else None
                    ),
                }
                if s is not None
                else None
            ),
        }
        for p, s in items
    ]
