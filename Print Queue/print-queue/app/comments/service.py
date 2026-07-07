"""Comment service: list, create, with notification dispatch on create."""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.comment import SubmissionComment
from app.models.submission import Submission
from app.models.user import User
from app.notifications.service import notify_comment_added

log = structlog.get_logger(__name__)

MAX_COMMENT_LENGTH = 2000


async def list_for_submission(db: AsyncSession, submission_id) -> list[SubmissionComment]:
    result = await db.execute(
        select(SubmissionComment)
        .where(SubmissionComment.submission_id == submission_id)
        .order_by(SubmissionComment.created_at.asc())
    )
    return list(result.scalars().all())


async def create(
    db: AsyncSession,
    settings: Settings,
    *,
    submission: Submission,
    author: User,
    body: str,
) -> SubmissionComment:
    body = body.strip()
    if not body:
        raise ValueError("Comment body cannot be empty")
    if len(body) > MAX_COMMENT_LENGTH:
        raise ValueError(f"Comment exceeds {MAX_COMMENT_LENGTH} characters")

    comment = SubmissionComment(
        submission_id=submission.id,
        author_id=author.id,
        author_email_at_write=author.email,
        body=body,
    )
    db.add(comment)
    await db.commit()
    await db.refresh(comment)
    log.info(
        "comment.created",
        submission_id=str(submission.id),
        author=author.email,
    )

    try:
        await notify_comment_added(
            db,
            settings,
            submission=submission,
            author=author,
            body=body,
        )
    except Exception as e:
        log.warning("comment.notify.error", error=str(e))

    return comment
