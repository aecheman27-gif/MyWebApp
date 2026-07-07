"""SubmissionEvent — append-only audit trail.

Every meaningful change to a submission gets a row here, so we can show
history and debug "why is this submission in this state."
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.submission import SubmissionStatus


class EventType(enum.StrEnum):
    CREATED = "CREATED"
    EDITED = "EDITED"
    STATUS_CHANGED = "STATUS_CHANGED"
    FILE_DOWNLOADED = "FILE_DOWNLOADED"
    DELETED = "DELETED"


class SubmissionEvent(Base):
    __tablename__ = "submission_events"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    submission_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("submissions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[EventType] = mapped_column(
        Enum(EventType, name="submission_event_type"), nullable=False
    )
    from_status: Mapped[SubmissionStatus | None] = mapped_column(
        Enum(SubmissionStatus, name="submission_status"), nullable=True
    )
    to_status: Mapped[SubmissionStatus | None] = mapped_column(
        Enum(SubmissionStatus, name="submission_status"), nullable=True
    )
    by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # JSONB on Postgres for indexability; JSON on SQLite (tests).
    event_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
