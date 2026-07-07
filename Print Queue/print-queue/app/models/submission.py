"""Submission model — a single print request in the queue."""

from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class SubmissionMaterial(enum.StrEnum):
    PLA = "PLA"
    PETG = "PETG"
    ABS = "ABS"
    TPU = "TPU"
    ASA = "ASA"


class SubmissionPriority(enum.StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    RUSH = "RUSH"

    @property
    def sort_order(self) -> int:
        """Higher = more urgent. Used for ORDER BY priority DESC."""
        return {"LOW": 0, "NORMAL": 1, "HIGH": 2, "RUSH": 3}[self.value]


class SubmissionStatus(enum.StrEnum):
    QUEUED = "QUEUED"
    SLICING = "SLICING"
    PRINTING = "PRINTING"
    DONE = "DONE"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"

    @property
    def is_active(self) -> bool:
        return self in {
            SubmissionStatus.QUEUED,
            SubmissionStatus.SLICING,
            SubmissionStatus.PRINTING,
        }

    @property
    def is_in_progress(self) -> bool:
        return self in {SubmissionStatus.SLICING, SubmissionStatus.PRINTING}

    @property
    def is_completed(self) -> bool:
        return self in {SubmissionStatus.DONE, SubmissionStatus.CANCELLED, SubmissionStatus.FAILED}


class Submission(Base):
    __tablename__ = "submissions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    submitter_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    part_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    material: Mapped[SubmissionMaterial] = mapped_column(
        Enum(SubmissionMaterial, name="submission_material"),
        nullable=False,
        default=SubmissionMaterial.PLA,
    )
    priority: Mapped[SubmissionPriority] = mapped_column(
        Enum(SubmissionPriority, name="submission_priority"),
        nullable=False,
        default=SubmissionPriority.NORMAL,
    )
    status: Mapped[SubmissionStatus] = mapped_column(
        Enum(SubmissionStatus, name="submission_status"),
        nullable=False,
        default=SubmissionStatus.QUEUED,
        index=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("files.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Operator-set optional pin: this submission should be printed on this
    # specific printer. NULL means "any available".
    target_printer_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("printers.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Set by M5 auto-bind when a print starts. Cleared when print completes.
    current_printer_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("printers.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    submitter = relationship("User", lazy="joined")
    file = relationship("StoredFile", lazy="joined")

    @property
    def filename_hint(self) -> str:
        """Suggested filename for the operator to save the sliced .3mf as.

        Used for the auto-bind feature in M5: when the printer reports
        subtask_name starting with `sub-<this-id>-`, this submission gets
        bound to the active print.
        """
        safe_name = "".join(c if c.isalnum() else "-" for c in self.part_name.lower())
        safe_name = safe_name.strip("-")[:40] or "part"
        return f"sub-{str(self.id)[:8]}-{safe_name}.3mf"
