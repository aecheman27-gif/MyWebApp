"""Submission business logic.

Routes are thin wrappers around these functions; tests cover these
directly so they don't have to go through HTTP for unit-level checks.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import structlog
from fastapi import HTTPException, status
from sqlalchemy import case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.file import StoredFile
from app.models.submission import (
    Submission,
    SubmissionPriority,
    SubmissionStatus,
)
from app.models.submission_event import EventType, SubmissionEvent
from app.models.user import User
from app.storage.base import FileStorage
from app.submissions.schemas import SubmissionCreate, SubmissionEdit

log = structlog.get_logger(__name__)

QueueFilter = Literal["mine", "active", "in_progress", "completed", "all"]

# Priority sort key — RUSH first, LOW last.
_PRIORITY_SORT = case(
    {
        SubmissionPriority.RUSH: 3,
        SubmissionPriority.HIGH: 2,
        SubmissionPriority.NORMAL: 1,
        SubmissionPriority.LOW: 0,
    },
    value=Submission.priority,
)

ACCEPTABLE_EXTENSIONS = (".step", ".stp", ".stl")


def _is_acceptable_filename(name: str) -> bool:
    return name.lower().endswith(ACCEPTABLE_EXTENSIONS)


async def create_submission(
    db: AsyncSession,
    settings: Settings,
    storage: FileStorage,
    submitter: User,
    data: SubmissionCreate,
    file_bytes: bytes | None,
    file_name: str | None,
    file_mime_type: str = "application/octet-stream",
) -> Submission:
    """Create a submission, optionally with an attached STEP/STL file."""
    file_id: UUID | None = None
    if file_bytes is not None and file_name:
        if len(file_bytes) > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File exceeds {settings.max_upload_mb} MB limit",
            )
        if not _is_acceptable_filename(file_name):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File must be .step, .stp, or .stl",
            )

        result = await storage.save(file_bytes, file_name, file_mime_type)
        stored = StoredFile(
            storage_backend=result.storage_backend,
            storage_key=result.storage_key,
            original_filename=file_name,
            size_bytes=result.size_bytes,
            mime_type=result.mime_type,
            sha256=result.sha256,
            expires_at=datetime.now(UTC) + timedelta(days=settings.file_retention_days),
        )
        db.add(stored)
        await db.flush()
        file_id = stored.id

    sub = Submission(
        submitter_id=submitter.id,
        part_name=data.part_name,
        description=data.description,
        material=data.material,
        priority=data.priority,
        notes=data.notes,
        file_id=file_id,
    )
    db.add(sub)
    await db.flush()

    db.add(
        SubmissionEvent(
            submission_id=sub.id,
            event_type=EventType.CREATED,
            to_status=sub.status,
            by_user_id=submitter.id,
            event_metadata={"part_name": data.part_name},
        )
    )

    await db.commit()
    await db.refresh(sub)
    log.info("submission.created", id=str(sub.id), by=submitter.email)
    return sub


async def edit_submission(
    db: AsyncSession,
    submission: Submission,
    by_user: User,
    data: SubmissionEdit,
) -> Submission:
    """Apply edits to an existing submission and record the event."""
    before = {
        "part_name": submission.part_name,
        "description": submission.description,
        "material": submission.material.value,
        "priority": submission.priority.value,
        "notes": submission.notes,
    }
    submission.part_name = data.part_name
    submission.description = data.description
    submission.material = data.material
    submission.priority = data.priority
    submission.notes = data.notes

    after = {
        "part_name": submission.part_name,
        "description": submission.description,
        "material": submission.material.value,
        "priority": submission.priority.value,
        "notes": submission.notes,
    }
    changes = {k: {"from": before[k], "to": after[k]} for k in before if before[k] != after[k]}

    db.add(
        SubmissionEvent(
            submission_id=submission.id,
            event_type=EventType.EDITED,
            by_user_id=by_user.id,
            event_metadata={"changes": changes} if changes else None,
        )
    )
    await db.commit()
    await db.refresh(submission)
    log.info("submission.edited", id=str(submission.id), by=by_user.email, changes=list(changes))
    return submission


async def change_status(
    db: AsyncSession,
    submission: Submission,
    by_user: User,
    to_status: SubmissionStatus,
    event_metadata: dict[str, Any] | None = None,
    settings=None,
) -> Submission:
    """Operator-initiated status change. No transition graph enforced in M2
    (operator can move between any states); M5 tightens this for auto-bind.

    If `settings` is provided, fires a notification dispatch after the commit.
    """
    from_status = submission.status
    if from_status == to_status:
        return submission
    submission.status = to_status
    db.add(
        SubmissionEvent(
            submission_id=submission.id,
            event_type=EventType.STATUS_CHANGED,
            from_status=from_status,
            to_status=to_status,
            by_user_id=by_user.id,
            event_metadata=event_metadata,
        )
    )
    await db.commit()
    await db.refresh(submission)
    log.info(
        "submission.status_changed",
        id=str(submission.id),
        by=by_user.email,
        from_=from_status.value,
        to=to_status.value,
    )
    if settings is not None:
        from app.notifications.service import notify_status_changed

        await notify_status_changed(
            db,
            settings,
            submission=submission,
            from_status=from_status,
            to_status=to_status,
        )
    return submission


async def delete_submission(
    db: AsyncSession,
    storage: FileStorage,
    submission: Submission,
    by_user: User,
) -> None:
    """Delete a submission and its attached file, if any."""
    file_to_delete: tuple[str, str] | None = None
    if submission.file is not None:
        file_to_delete = (submission.file.storage_backend, submission.file.storage_key)

    db.add(
        SubmissionEvent(
            submission_id=submission.id,
            event_type=EventType.DELETED,
            from_status=submission.status,
            by_user_id=by_user.id,
            event_metadata={"part_name": submission.part_name},
        )
    )
    # Audit row stays via ondelete=CASCADE? No — we use CASCADE on the FK.
    # Actually we want the audit row to survive, so we'll detach instead.
    # For M2, simpler: delete the audit row alongside via cascade. We can
    # reconsider once we have an audit-log UI to display.
    await db.delete(submission)
    await db.commit()

    if file_to_delete is not None:
        await storage.delete(file_to_delete[1])
    log.info("submission.deleted", id=str(submission.id), by=by_user.email)


async def get_submission(db: AsyncSession, submission_id: UUID) -> Submission | None:
    result = await db.execute(select(Submission).where(Submission.id == submission_id))
    return result.scalar_one_or_none()


async def list_submissions(
    db: AsyncSession,
    *,
    filter_: QueueFilter = "active",
    user: User | None = None,
    search: str | None = None,
) -> Sequence[Submission]:
    """Return submissions for the queue page, sorted and filtered."""
    stmt = select(Submission)

    if filter_ == "mine":
        if user is None:
            return []
        stmt = stmt.where(Submission.submitter_id == user.id)
    elif filter_ == "active":
        stmt = stmt.where(
            Submission.status.in_(
                [
                    SubmissionStatus.QUEUED,
                    SubmissionStatus.SLICING,
                    SubmissionStatus.PRINTING,
                ]
            )
        )
    elif filter_ == "in_progress":
        stmt = stmt.where(
            Submission.status.in_([SubmissionStatus.SLICING, SubmissionStatus.PRINTING])
        )
    elif filter_ == "completed":
        stmt = stmt.where(
            Submission.status.in_(
                [
                    SubmissionStatus.DONE,
                    SubmissionStatus.CANCELLED,
                    SubmissionStatus.FAILED,
                ]
            )
        )
    # filter_ == "all": no extra filter.

    if search:
        like = f"%{search.lower()}%"
        # Match against part name, description, or notes (case-insensitive).
        stmt = stmt.where(
            or_(
                Submission.part_name.ilike(like),
                Submission.description.ilike(like),
                Submission.notes.ilike(like),
            )
        )

    # Sort: priority DESC, then created_at ASC (older = next in line within priority).
    stmt = stmt.order_by(_PRIORITY_SORT.desc(), Submission.created_at.asc())

    result = await db.execute(stmt)
    return result.scalars().all()
