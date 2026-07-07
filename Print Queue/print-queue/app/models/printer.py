"""Printer and PrinterState models.

A `Printer` is a configured printer in the system. It has a stable id,
display name, location, and a Bambu serial number (used to match incoming
MQTT messages on the topic `device/<SERIAL>/report`).

`PrinterState` is the latest telemetry snapshot — one row per printer,
upserted by the bridge. We keep this as a dedicated table (rather than
embedding columns into `printers`) so the snapshot can be rewritten
atomically without touching the printer's static metadata.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PrinterStatus(enum.StrEnum):
    """Normalized printer state. Mapped from Bambu's `gcode_state` field."""

    IDLE = "IDLE"
    PREPARING = "PREPARING"
    PRINTING = "PRINTING"
    PAUSED = "PAUSED"
    FINISHED = "FINISHED"
    FAILED = "FAILED"
    OFFLINE = "OFFLINE"  # bridge can't reach printer or hasn't reported recently


class Printer(Base):
    __tablename__ = "printers"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    # Short human-friendly identifier used in the UI and `bridge` config (e.g. "P1", "P2").
    slug: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    location: Mapped[str | None] = mapped_column(String(120), nullable=True)
    serial: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    state = relationship("PrinterState", uselist=False, lazy="joined", back_populates="printer")


class PrinterState(Base):
    """Latest snapshot for one printer. One row per printer."""

    __tablename__ = "printer_state"

    printer_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("printers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[PrinterStatus] = mapped_column(
        Enum(PrinterStatus, name="printer_status"),
        nullable=False,
        default=PrinterStatus.OFFLINE,
    )
    current_file: Mapped[str | None] = mapped_column(String(300), nullable=True)
    percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    remaining_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    layer: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_layers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    nozzle_temp: Mapped[float | None] = mapped_column(Float, nullable=True)
    nozzle_target: Mapped[float | None] = mapped_column(Float, nullable=True)
    bed_temp: Mapped[float | None] = mapped_column(Float, nullable=True)
    bed_target: Mapped[float | None] = mapped_column(Float, nullable=True)
    wifi_signal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Submission bound to the current print (set by M5 auto-bind).
    current_submission_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("submissions.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Full raw MQTT report kept for debugging. JSON on SQLite; JSONB on Postgres.
    raw: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )

    printer = relationship("Printer", back_populates="state", lazy="joined")
    current_submission = relationship(
        "Submission", lazy="joined", foreign_keys=[current_submission_id]
    )
