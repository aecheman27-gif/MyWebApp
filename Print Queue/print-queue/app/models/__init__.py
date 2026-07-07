"""ORM models. All models must be imported here so Alembic and test setup
that use Base.metadata.create_all can discover them.
"""

from app.models.comment import SubmissionComment
from app.models.file import StoredFile
from app.models.magic_link import MagicLink
from app.models.printer import Printer, PrinterState, PrinterStatus
from app.models.submission import (
    Submission,
    SubmissionMaterial,
    SubmissionPriority,
    SubmissionStatus,
)
from app.models.submission_event import EventType, SubmissionEvent
from app.models.user import User, UserRole

__all__ = [
    "EventType",
    "MagicLink",
    "Printer",
    "PrinterState",
    "PrinterStatus",
    "StoredFile",
    "Submission",
    "SubmissionComment",
    "SubmissionEvent",
    "SubmissionMaterial",
    "SubmissionPriority",
    "SubmissionStatus",
    "User",
    "UserRole",
]
