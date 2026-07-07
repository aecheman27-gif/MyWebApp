"""Permission helpers for submissions.

The rules:
- Operators can do anything to anything.
- Submitters can create new submissions.
- Submitters can edit/delete their OWN submissions, but only while the
  submission is still in the QUEUED state. Once an operator starts
  processing it, the submitter can no longer mutate it.
- Anyone with a valid session can VIEW the queue (read-only).
- Submitters can download their own files; operators can download any.
- Only operators can change status.
"""

from __future__ import annotations

from app.models.submission import Submission, SubmissionStatus
from app.models.user import User, UserRole


def is_operator(user: User) -> bool:
    return user.role == UserRole.operator


def can_view_queue(user: User) -> bool:
    # Any authenticated user can view; we keep the function for symmetry.
    return True


def can_create_submission(user: User) -> bool:
    return True


def can_edit_submission(user: User, submission: Submission) -> bool:
    if is_operator(user):
        return True
    if submission.submitter_id != user.id:
        return False
    return submission.status == SubmissionStatus.QUEUED


def can_delete_submission(user: User, submission: Submission) -> bool:
    # Same rule as edit.
    return can_edit_submission(user, submission)


def can_change_status(user: User) -> bool:
    return is_operator(user)


def can_download_file(user: User, submission: Submission) -> bool:
    if is_operator(user):
        return True
    return submission.submitter_id == user.id


def can_view(user: User, submission: Submission) -> bool:
    """Whether the user can see a specific submission's detail page and comments.
    Operators see any; submitters see only their own."""
    if is_operator(user):
        return True
    return submission.submitter_id == user.id


def can_comment(user: User, submission: Submission) -> bool:
    """Same scope as view — anyone who can see it can comment."""
    return can_view(user, submission)
