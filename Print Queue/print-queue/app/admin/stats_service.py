"""Stats calculations for the admin dashboard.

We have:
- submissions (with material, priority, timestamps)
- submission_events (status changes with from_status, to_status, created_at)

From these we derive:
- counts by status, material
- failure rate
- mean/total print durations (the gap between QUEUED→PRINTING and
  PRINTING→DONE/FAILED events on the same submission)
- per-user / per-printer breakdowns
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.printer import Printer
from app.models.submission import (
    Submission,
    SubmissionStatus,
)
from app.models.submission_event import EventType, SubmissionEvent
from app.models.user import User


@dataclass
class StatsResult:
    range_start: datetime
    range_end: datetime
    submissions_total: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    by_material: dict[str, int] = field(default_factory=dict)
    by_submitter: dict[str, int] = field(default_factory=dict)
    by_printer: dict[str, int] = field(default_factory=dict)
    completed_count: int = 0
    failed_count: int = 0
    cancelled_count: int = 0
    failure_rate_pct: float = 0.0
    total_print_minutes: int = 0
    completed_print_minutes: int = 0
    avg_print_minutes: float = 0.0
    longest_print_minutes: int = 0


def _aware_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def compute_stats(
    db: AsyncSession,
    *,
    range_start: datetime,
    range_end: datetime,
) -> StatsResult:
    """All stats are derived from submissions whose created_at falls in the range."""
    result = await db.execute(
        select(Submission).where(
            Submission.created_at >= range_start,
            Submission.created_at < range_end,
        )
    )
    submissions = list(result.scalars().all())

    by_status: dict[str, int] = defaultdict(int)
    by_material: dict[str, int] = defaultdict(int)
    by_submitter: dict[str, int] = defaultdict(int)
    by_printer: dict[str, int] = defaultdict(int)

    # Cache lookups so we don't issue 1 query per submission.
    user_cache: dict[Any, str] = {}
    printer_cache: dict[Any, str] = {}

    async def _user_email(uid):
        if uid is None:
            return "(unknown)"
        if uid in user_cache:
            return user_cache[uid]
        u = await db.get(User, uid)
        email = u.email if u else "(deleted)"
        user_cache[uid] = email
        return email

    async def _printer_name(pid):
        if pid is None:
            return None
        if pid in printer_cache:
            return printer_cache[pid]
        p = await db.get(Printer, pid)
        name = p.name if p else "(deleted)"
        printer_cache[pid] = name
        return name

    completed_count = 0
    failed_count = 0
    cancelled_count = 0
    durations: list[int] = []
    longest = 0

    for s in submissions:
        by_status[s.status.value] += 1
        by_material[s.material.value] += 1
        by_submitter[await _user_email(s.submitter_id)] += 1
        printer_label = await _printer_name(s.current_printer_id)
        if printer_label:
            by_printer[printer_label] += 1

        if s.status == SubmissionStatus.DONE:
            completed_count += 1
        elif s.status == SubmissionStatus.FAILED:
            failed_count += 1
        elif s.status == SubmissionStatus.CANCELLED:
            cancelled_count += 1

    # Print durations: derive from event timestamps.
    if submissions:
        sub_ids = [s.id for s in submissions]
        ev_result = await db.execute(
            select(SubmissionEvent)
            .where(
                SubmissionEvent.submission_id.in_(sub_ids),
                SubmissionEvent.event_type == EventType.STATUS_CHANGED,
            )
            .order_by(SubmissionEvent.created_at.asc())
        )
        events_by_sub: dict[Any, list[SubmissionEvent]] = defaultdict(list)
        for ev in ev_result.scalars().all():
            events_by_sub[ev.submission_id].append(ev)

        for _sub_id, events in events_by_sub.items():
            start_ts = None
            for ev in events:
                if ev.to_status == SubmissionStatus.PRINTING:
                    start_ts = _aware_utc(ev.created_at)
                elif (
                    ev.to_status
                    in (
                        SubmissionStatus.DONE,
                        SubmissionStatus.FAILED,
                    )
                    and start_ts is not None
                ):
                    dur = int((_aware_utc(ev.created_at) - start_ts).total_seconds() // 60)
                    if dur >= 0:
                        durations.append(dur)
                        longest = max(longest, dur)
                    start_ts = None

    total_print_minutes = sum(durations)
    avg_print_minutes = sum(durations) / len(durations) if durations else 0.0

    settled = completed_count + failed_count
    failure_rate_pct = (failed_count / settled * 100) if settled else 0.0

    return StatsResult(
        range_start=range_start,
        range_end=range_end,
        submissions_total=len(submissions),
        by_status=dict(by_status),
        by_material=dict(by_material),
        by_submitter=dict(by_submitter),
        by_printer=dict(by_printer),
        completed_count=completed_count,
        failed_count=failed_count,
        cancelled_count=cancelled_count,
        failure_rate_pct=failure_rate_pct,
        total_print_minutes=total_print_minutes,
        completed_print_minutes=total_print_minutes,
        avg_print_minutes=avg_print_minutes,
        longest_print_minutes=longest,
    )


def format_minutes(minutes: int | float) -> str:
    """Pretty-print a duration in minutes as 'Xh Ym' or 'Ym'."""
    m = int(minutes)
    if m < 60:
        return f"{m}m"
    return f"{m // 60}h {m % 60}m"


def default_range_last_30_days() -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    return now - timedelta(days=30), now


async def csv_export_submissions(
    db: AsyncSession,
    range_start: datetime,
    range_end: datetime,
) -> str:
    """Return a CSV string of submissions in the range (one row per submission)."""
    import csv
    import io

    result = await db.execute(
        select(Submission)
        .where(
            Submission.created_at >= range_start,
            Submission.created_at < range_end,
        )
        .order_by(Submission.created_at.asc())
    )
    submissions = list(result.scalars().all())

    user_cache: dict[Any, str] = {}
    printer_cache: dict[Any, str] = {}

    async def _user_email(uid):
        if uid is None:
            return ""
        if uid in user_cache:
            return user_cache[uid]
        u = await db.get(User, uid)
        email = u.email if u else "(deleted)"
        user_cache[uid] = email
        return email

    async def _printer_name(pid):
        if pid is None:
            return ""
        if pid in printer_cache:
            return printer_cache[pid]
        p = await db.get(Printer, pid)
        name = p.name if p else "(deleted)"
        printer_cache[pid] = name
        return name

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "id",
            "created_at",
            "submitter_email",
            "part_name",
            "status",
            "material",
            "priority",
            "printer",
            "description",
        ]
    )
    for s in submissions:
        writer.writerow(
            [
                str(s.id),
                s.created_at.isoformat(),
                await _user_email(s.submitter_id),
                s.part_name,
                s.status.value,
                s.material.value,
                s.priority.value,
                await _printer_name(s.current_printer_id),
                (s.description or "").replace("\n", " "),
            ]
        )

    return output.getvalue()
