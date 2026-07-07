"""Central dispatch for notifications.

Status transitions and comments call into here; the service decides which
channels (email, slack, discord) to actually use based on settings, user
preferences, and event type.

Designed to be defensive: every send is wrapped, failures get logged but
never raised, so a failing webhook can't roll back a status transition.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.printer import Printer
from app.models.submission import Submission, SubmissionStatus
from app.models.user import User, UserRole
from app.notifications.email import send_comment_email, send_status_change_email
from app.notifications.webhooks import notify_failure

log = structlog.get_logger(__name__)


# Statuses that should send the submitter an email when reached.
_SUBMITTER_NOTIFY_STATUSES: set[SubmissionStatus] = {
    SubmissionStatus.PRINTING,
    SubmissionStatus.DONE,
    SubmissionStatus.FAILED,
    SubmissionStatus.CANCELLED,
}


async def notify_status_changed(
    db: AsyncSession,
    settings: Settings,
    *,
    submission: Submission,
    from_status: SubmissionStatus | None,
    to_status: SubmissionStatus,
    actor_note: str | None = None,
) -> None:
    """Fan out a status-change notification.

    - Email to submitter (if they have email_notifications enabled and the
      new status is in the notify set).
    - Webhooks to Slack/Discord on FAILED (operator wants to know NOW).
    """
    # The submission may not have its submitter loaded — fetch fresh.
    submitter = await db.get(User, submission.submitter_id)
    submission_url = f"/submissions/{submission.id}"

    try:
        if (
            submitter is not None
            and submitter.email_notifications
            and to_status in _SUBMITTER_NOTIFY_STATUSES
            and to_status != from_status
        ):
            await send_status_change_email(
                settings,
                to_email=submitter.email,
                part_name=submission.part_name,
                new_status=to_status.value,
                site_url=settings.site_url,
                submission_url_path=submission_url,
                actor_note=actor_note,
            )
    except Exception as e:
        log.warning("notify.status_email.error", error=str(e))

    try:
        if to_status == SubmissionStatus.FAILED:
            printer_name = None
            error_code = None
            if submission.current_printer_id:
                p = await db.get(Printer, submission.current_printer_id)
                if p is not None:
                    printer_name = p.name
            submitter_email = submitter.email if submitter else "unknown"
            await notify_failure(
                settings,
                submission_part_name=submission.part_name,
                submitter_email=submitter_email,
                printer_name=printer_name,
                error_code=error_code,
                site_url=settings.site_url,
                submission_url_path=submission_url,
            )
    except Exception as e:
        log.warning("notify.failure_webhook.error", error=str(e))


async def notify_comment_added(
    db: AsyncSession,
    settings: Settings,
    *,
    submission: Submission,
    author: User,
    body: str,
) -> None:
    """When a comment is added, email the relevant other party.

    - If a submitter commented: email all active operators.
    - If an operator commented: email the submitter.
    The author never gets a copy of their own comment.
    """
    submitter = await db.get(User, submission.submitter_id)
    if submitter is None:
        return
    submission_url = f"/submissions/{submission.id}"

    recipients: list[User] = []
    if author.role == UserRole.operator:
        if submitter.id != author.id and submitter.email_notifications:
            recipients.append(submitter)
    else:  # submitter commented
        result = await db.execute(
            select(User).where(
                User.role == UserRole.operator,
                User.is_active.is_(True),
                User.email_notifications.is_(True),
            )
        )
        for op in result.scalars().all():
            if op.id != author.id:
                recipients.append(op)

    for recipient in recipients:
        try:
            await send_comment_email(
                settings,
                to_email=recipient.email,
                part_name=submission.part_name,
                author_email=author.email,
                body=body,
                site_url=settings.site_url,
                submission_url_path=submission_url,
            )
        except Exception as e:
            log.warning(
                "notify.comment_email.error",
                error=str(e),
                recipient=recipient.email,
            )
