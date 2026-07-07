"""Server-side telemetry processing and auto-bind.

Two main entry points:
- `ingest_telemetry`: store the latest snapshot, fan out to SSE subscribers
- `_maybe_autobind` (internal): when a print starts/finishes/fails, link the
  active print to a submission via the `sub-<id>-` filename convention.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.printer import Printer, PrinterState, PrinterStatus
from app.models.submission import Submission, SubmissionStatus
from app.models.submission_event import EventType, SubmissionEvent
from app.printers.pubsub import broadcaster
from app.printers.schemas import TelemetryIn

log = structlog.get_logger(__name__)

# Matches the prefix the operator types when saving a sliced .3mf:
# `sub-<first 8 hex chars of submission UUID>-anything.3mf`
_SUB_PREFIX = re.compile(r"^sub-([0-9a-f]{8})-", re.IGNORECASE)


async def get_or_create_printer(
    db: AsyncSession,
    slug: str,
    serial: str,
) -> Printer:
    """Bridge announces a printer by slug+serial; we ensure it exists.

    This lets the operator register printers either:
    - implicitly: bridge starts up with a printer config, server auto-creates
      the row with sensible defaults
    - explicitly: admin UI later (not yet built)
    """
    result = await db.execute(select(Printer).where(Printer.slug == slug))
    printer = result.scalar_one_or_none()
    if printer is None:
        printer = Printer(slug=slug, name=slug, serial=serial)
        db.add(printer)
        await db.flush()
        log.info("printer.auto_registered", slug=slug, serial=serial)
    elif printer.serial != serial:
        log.warning(
            "printer.serial_mismatch",
            slug=slug,
            existing=printer.serial,
            incoming=serial,
        )
    return printer


async def ingest_telemetry(db: AsyncSession, t: TelemetryIn) -> PrinterState:
    """Store a telemetry snapshot, run autobind, fan out via SSE."""
    printer = await get_or_create_printer(db, t.printer_slug, t.printer_serial)

    state = await db.get(PrinterState, printer.id)
    prev_status = state.status if state else None
    prev_file = state.current_file if state else None

    if state is None:
        state = PrinterState(printer_id=printer.id, last_seen_at=t.ts, status=t.status)
        db.add(state)

    state.last_seen_at = t.ts
    state.status = t.status
    state.current_file = t.current_file
    state.percent = t.percent
    state.remaining_minutes = t.remaining_minutes
    state.layer = t.layer
    state.total_layers = t.total_layers
    state.nozzle_temp = t.nozzle_temp
    state.nozzle_target = t.nozzle_target
    state.bed_temp = t.bed_temp
    state.bed_target = t.bed_target
    state.wifi_signal = t.wifi_signal
    state.error_code = t.error_code
    state.raw = t.raw

    await _maybe_autobind(
        db,
        printer=printer,
        state=state,
        prev_status=prev_status,
        prev_file=prev_file,
        new_status=t.status,
        new_file=t.current_file,
        error_code=t.error_code,
    )

    await db.commit()
    await db.refresh(state)

    await broadcaster.publish(_state_to_dict(printer, state))
    return state


async def _maybe_autobind(
    db: AsyncSession,
    *,
    printer: Printer,
    state: PrinterState,
    prev_status: PrinterStatus | None,
    prev_file: str | None,
    new_status: PrinterStatus,
    new_file: str | None,
    error_code: int | None,
) -> None:
    """Update the linked submission based on printer state transitions.

    Rules (kept simple, refined later if needed):
    - Transition into PRINTING with a `sub-<id>-...` filename: look up the
      submission, flip Queued/Slicing → Printing, link it.
    - Transition into FINISHED while bound: bound submission → Done, clear link.
    - Transition into FAILED (or nonzero error_code) while bound:
      bound submission → Failed.
    """
    transitioned_to_printing = (
        new_status == PrinterStatus.PRINTING and prev_status != PrinterStatus.PRINTING
    )
    transitioned_to_finished = (
        new_status == PrinterStatus.FINISHED and prev_status == PrinterStatus.PRINTING
    )
    transitioned_to_failed = (
        new_status == PrinterStatus.FAILED and prev_status != PrinterStatus.FAILED
    )

    if transitioned_to_printing and new_file:
        submission = await _resolve_submission_from_filename(db, new_file)
        if submission is not None:
            await _bind_submission(db, state, submission, printer)

    elif transitioned_to_finished and state.current_submission_id:
        await _complete_bound_submission(db, state, SubmissionStatus.DONE, error_code=None)

    elif transitioned_to_failed and state.current_submission_id:
        await _complete_bound_submission(db, state, SubmissionStatus.FAILED, error_code=error_code)

    elif error_code and state.current_submission_id and new_status == PrinterStatus.PRINTING:
        # Printer reports an error mid-print without an explicit state change.
        # Don't flip status — the operator/printer will resolve. Just log it.
        log.info(
            "autobind.error_during_print",
            printer=printer.slug,
            submission_id=str(state.current_submission_id),
            error_code=error_code,
        )


async def _resolve_submission_from_filename(
    db: AsyncSession,
    filename: str,
) -> Submission | None:
    m = _SUB_PREFIX.match(filename)
    if not m:
        return None
    prefix_hex = m.group(1).lower()
    # Match against UUIDs whose stringified form starts with the prefix.
    # Using LIKE on the cast id keeps the query portable across SQLite/Postgres.
    from sqlalchemy import cast
    from sqlalchemy.types import String as SAString

    stmt = select(Submission).where(cast(Submission.id, SAString).ilike(f"{prefix_hex}%"))
    result = await db.execute(stmt)
    candidates = result.scalars().all()
    if not candidates:
        log.info("autobind.no_match", filename=filename, prefix=prefix_hex)
        return None
    if len(candidates) > 1:
        log.warning(
            "autobind.ambiguous_prefix",
            filename=filename,
            prefix=prefix_hex,
            count=len(candidates),
        )
        return None
    return candidates[0]


async def _bind_submission(
    db: AsyncSession,
    state: PrinterState,
    submission: Submission,
    printer: Printer,
) -> None:
    state.current_submission_id = submission.id
    submission.current_printer_id = printer.id
    prev_status = submission.status
    if submission.status in (SubmissionStatus.QUEUED, SubmissionStatus.SLICING):
        submission.status = SubmissionStatus.PRINTING
        db.add(
            SubmissionEvent(
                submission_id=submission.id,
                event_type=EventType.STATUS_CHANGED,
                to_status=SubmissionStatus.PRINTING,
                event_metadata={"autobind": True, "printer_slug": printer.slug},
            )
        )
        # Fire notification (commit happens in caller; pass `db` so the
        # status the email reports matches what's about to be persisted).
        try:
            from app.config import get_settings
            from app.notifications.service import notify_status_changed

            await notify_status_changed(
                db,
                get_settings(),
                submission=submission,
                from_status=prev_status,
                to_status=SubmissionStatus.PRINTING,
                actor_note=f"Auto-detected on {printer.name}.",
            )
        except Exception as e:
            log.warning("autobind.notify.error", error=str(e))
    log.info(
        "autobind.bound",
        printer=printer.slug,
        submission_id=str(submission.id),
        part=submission.part_name,
    )


async def _complete_bound_submission(
    db: AsyncSession,
    state: PrinterState,
    final_status: SubmissionStatus,
    error_code: int | None,
) -> None:
    if state.current_submission_id is None:
        return
    submission = await db.get(Submission, state.current_submission_id)
    if submission is None:
        return
    prev = submission.status
    submission.status = final_status
    submission.current_printer_id = None
    state.current_submission_id = None
    db.add(
        SubmissionEvent(
            submission_id=submission.id,
            event_type=EventType.STATUS_CHANGED,
            from_status=prev,
            to_status=final_status,
            event_metadata={
                "autobind": True,
                "error_code": error_code,
            },
        )
    )
    log.info(
        "autobind.completed",
        submission_id=str(submission.id),
        final_status=final_status.value,
        error_code=error_code,
    )
    try:
        from app.config import get_settings
        from app.notifications.service import notify_status_changed

        actor_note = None
        if error_code:
            actor_note = f"Printer reported error code {error_code}."
        await notify_status_changed(
            db,
            get_settings(),
            submission=submission,
            from_status=prev,
            to_status=final_status,
            actor_note=actor_note,
        )
    except Exception as e:
        log.warning("autobind.notify.error", error=str(e))


def _state_to_dict(printer: Printer, state: PrinterState) -> dict:
    """Shape the SSE event payload."""
    return {
        "printer_id": str(printer.id),
        "slug": printer.slug,
        "name": printer.name,
        "location": printer.location,
        "last_seen_at": state.last_seen_at.isoformat() if state.last_seen_at else None,
        "status": state.status.value,
        "current_file": state.current_file,
        "percent": state.percent,
        "remaining_minutes": state.remaining_minutes,
        "layer": state.layer,
        "total_layers": state.total_layers,
        "nozzle_temp": state.nozzle_temp,
        "nozzle_target": state.nozzle_target,
        "bed_temp": state.bed_temp,
        "bed_target": state.bed_target,
        "wifi_signal": state.wifi_signal,
        "error_code": state.error_code,
        "current_submission_id": (
            str(state.current_submission_id) if state.current_submission_id else None
        ),
    }


async def mark_offline_if_stale(
    db: AsyncSession,
    *,
    stale_after_seconds: int = 30,
) -> int:
    """Flip any printer to OFFLINE if it hasn't reported in `stale_after_seconds`.

    Called periodically by a background task so the widget shows offline state
    even when no fresh telemetry is coming in.
    """
    now = datetime.now(UTC)
    result = await db.execute(select(Printer, PrinterState).join(PrinterState))
    flipped = 0
    for printer, state in result:
        if state.status == PrinterStatus.OFFLINE:
            continue
        last_seen = state.last_seen_at
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=UTC)
        if (now - last_seen).total_seconds() > stale_after_seconds:
            state.status = PrinterStatus.OFFLINE
            flipped += 1
            await broadcaster.publish(_state_to_dict(printer, state))
    if flipped:
        await db.commit()
        log.info("printers.marked_offline", count=flipped)
    return flipped


async def list_printer_states(db: AsyncSession) -> list[tuple[Printer, PrinterState | None]]:
    """All printers + their current state (None if no state row yet)."""
    result = await db.execute(
        select(Printer).where(Printer.enabled.is_(True)).order_by(Printer.slug)
    )
    printers = list(result.scalars().all())
    out: list[tuple[Printer, PrinterState | None]] = []
    for p in printers:
        s = await db.get(PrinterState, p.id)
        out.append((p, s))
    return out
