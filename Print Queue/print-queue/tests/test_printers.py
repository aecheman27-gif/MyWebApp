"""Server-side printer telemetry + autobind tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.printer import Printer, PrinterState, PrinterStatus
from app.models.submission import SubmissionStatus
from app.printers import service
from app.printers.schemas import TelemetryIn


def _make_telemetry(slug="P1", serial="ABC123", **overrides):
    base = {
        "printer_slug": slug,
        "printer_serial": serial,
        "ts": datetime.now(UTC),
        "status": PrinterStatus.IDLE,
        "current_file": None,
        "percent": None,
        "remaining_minutes": None,
        "layer": None,
        "total_layers": None,
        "nozzle_temp": None,
        "nozzle_target": None,
        "bed_temp": None,
        "bed_target": None,
        "wifi_signal": None,
        "error_code": None,
        "raw": None,
    }
    base.update(overrides)
    return TelemetryIn(**base)


@pytest.mark.asyncio
async def test_ingest_creates_printer_and_state(db_session):
    t = _make_telemetry(status=PrinterStatus.IDLE)
    await service.ingest_telemetry(db_session, t)

    printer = (await db_session.execute(select(Printer))).scalar_one()
    assert printer.slug == "P1"
    assert printer.serial == "ABC123"

    state = await db_session.get(PrinterState, printer.id)
    assert state is not None
    assert state.status == PrinterStatus.IDLE


@pytest.mark.asyncio
async def test_ingest_updates_existing_state(db_session):
    await service.ingest_telemetry(db_session, _make_telemetry(status=PrinterStatus.IDLE))
    await service.ingest_telemetry(
        db_session,
        _make_telemetry(status=PrinterStatus.PRINTING, percent=12.5),
    )
    printer = (await db_session.execute(select(Printer))).scalar_one()
    state = await db_session.get(PrinterState, printer.id)
    assert state.status == PrinterStatus.PRINTING
    assert state.percent == 12.5


@pytest.mark.asyncio
async def test_autobind_on_print_start(db_session, submitter, settings):
    """When the printer starts printing a file with `sub-<id>-` prefix,
    the corresponding submission auto-flips to PRINTING."""
    from app.storage import get_storage
    from app.submissions import service as sub_service
    from app.submissions.schemas import SubmissionCreate

    storage = get_storage()
    sub = await sub_service.create_submission(
        db_session,
        settings,
        storage,
        submitter,
        SubmissionCreate(part_name="bracket"),
        None,
        None,
    )

    prefix = str(sub.id)[:8]
    filename = f"sub-{prefix}-bracket.3mf"

    await service.ingest_telemetry(db_session, _make_telemetry(status=PrinterStatus.IDLE))
    await service.ingest_telemetry(
        db_session,
        _make_telemetry(
            status=PrinterStatus.PRINTING,
            current_file=filename,
            percent=0.5,
        ),
    )

    await db_session.refresh(sub)
    assert sub.status == SubmissionStatus.PRINTING
    state = (await db_session.execute(select(PrinterState))).scalar_one()
    assert state.current_submission_id == sub.id


@pytest.mark.asyncio
async def test_autobind_on_print_finish(db_session, submitter, settings):
    from app.storage import get_storage
    from app.submissions import service as sub_service
    from app.submissions.schemas import SubmissionCreate

    storage = get_storage()
    sub = await sub_service.create_submission(
        db_session,
        settings,
        storage,
        submitter,
        SubmissionCreate(part_name="bracket"),
        None,
        None,
    )
    prefix = str(sub.id)[:8]
    filename = f"sub-{prefix}-bracket.3mf"

    await service.ingest_telemetry(db_session, _make_telemetry(status=PrinterStatus.IDLE))
    await service.ingest_telemetry(
        db_session,
        _make_telemetry(status=PrinterStatus.PRINTING, current_file=filename),
    )
    await service.ingest_telemetry(
        db_session,
        _make_telemetry(status=PrinterStatus.FINISHED, current_file=filename),
    )

    await db_session.refresh(sub)
    assert sub.status == SubmissionStatus.DONE
    state = (await db_session.execute(select(PrinterState))).scalar_one()
    assert state.current_submission_id is None


@pytest.mark.asyncio
async def test_autobind_on_print_failed_with_error_code(db_session, submitter, settings):
    from app.storage import get_storage
    from app.submissions import service as sub_service
    from app.submissions.schemas import SubmissionCreate

    storage = get_storage()
    sub = await sub_service.create_submission(
        db_session,
        settings,
        storage,
        submitter,
        SubmissionCreate(part_name="bracket"),
        None,
        None,
    )
    prefix = str(sub.id)[:8]
    filename = f"sub-{prefix}-bracket.3mf"

    await service.ingest_telemetry(db_session, _make_telemetry(status=PrinterStatus.IDLE))
    await service.ingest_telemetry(
        db_session,
        _make_telemetry(status=PrinterStatus.PRINTING, current_file=filename),
    )
    await service.ingest_telemetry(
        db_session,
        _make_telemetry(status=PrinterStatus.FAILED, current_file=filename, error_code=12345),
    )

    await db_session.refresh(sub)
    assert sub.status == SubmissionStatus.FAILED


@pytest.mark.asyncio
async def test_autobind_no_match_for_unknown_prefix(db_session):
    """Filename without a `sub-` prefix is silently ignored."""
    await service.ingest_telemetry(db_session, _make_telemetry(status=PrinterStatus.IDLE))
    await service.ingest_telemetry(
        db_session,
        _make_telemetry(
            status=PrinterStatus.PRINTING,
            current_file="random_print_unrelated.3mf",
        ),
    )
    state = (await db_session.execute(select(PrinterState))).scalar_one()
    assert state.current_submission_id is None


@pytest.mark.asyncio
async def test_mark_offline_if_stale(db_session):
    # Insert telemetry with a manually-set old last_seen.
    await service.ingest_telemetry(db_session, _make_telemetry(status=PrinterStatus.IDLE))
    state = (await db_session.execute(select(PrinterState))).scalar_one()
    state.last_seen_at = datetime.now(UTC) - timedelta(seconds=120)
    await db_session.commit()

    flipped = await service.mark_offline_if_stale(db_session, stale_after_seconds=30)
    assert flipped == 1
    state = (await db_session.execute(select(PrinterState))).scalar_one()
    assert state.status == PrinterStatus.OFFLINE


@pytest.mark.asyncio
async def test_telemetry_endpoint_requires_token(client, settings, monkeypatch):
    """When BRIDGE_SHARED_TOKEN is set, requests without it get 401."""
    monkeypatch.setattr(settings, "bridge_shared_token", "secret-token")
    payload = {
        "printer_slug": "P1",
        "printer_serial": "ABC",
        "ts": datetime.now(UTC).isoformat(),
        "status": "IDLE",
    }
    r = await client.post("/internal/telemetry", json=payload)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_telemetry_endpoint_accepts_correct_token(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "bridge_shared_token", "secret-token")
    payload = {
        "printer_slug": "P1",
        "printer_serial": "ABC",
        "ts": datetime.now(UTC).isoformat(),
        "status": "IDLE",
    }
    r = await client.post(
        "/internal/telemetry",
        json=payload,
        headers={"X-Bridge-Token": "secret-token"},
    )
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_admin_printers_lists_state(client, operator, db_session):
    from app.auth.service import SESSION_COOKIE_NAME, create_session_cookie
    from app.config import get_settings

    await service.ingest_telemetry(db_session, _make_telemetry(status=PrinterStatus.PRINTING))

    cookies = {SESSION_COOKIE_NAME: create_session_cookie(get_settings(), operator.id)}
    r = await client.get("/admin/printers", cookies=cookies)
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["slug"] == "P1"
    assert data[0]["state"]["status"] == "PRINTING"
