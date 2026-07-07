"""Pydantic schemas for the telemetry payload from the bridge."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.printer import PrinterStatus


class TelemetryIn(BaseModel):
    """Single telemetry snapshot from the bridge.

    The bridge does the normalization (Bambu `gcode_state` → our PrinterStatus,
    etc.) so the server just stores what comes in.
    """

    model_config = ConfigDict(extra="ignore")

    printer_slug: str = Field(..., min_length=1, max_length=32)
    printer_serial: str = Field(..., min_length=1, max_length=64)
    ts: datetime
    status: PrinterStatus
    current_file: str | None = None
    percent: float | None = Field(default=None, ge=0.0, le=100.0)
    remaining_minutes: int | None = Field(default=None, ge=0)
    layer: int | None = Field(default=None, ge=0)
    total_layers: int | None = Field(default=None, ge=0)
    nozzle_temp: float | None = None
    nozzle_target: float | None = None
    bed_temp: float | None = None
    bed_target: float | None = None
    wifi_signal: int | None = None
    error_code: int | None = None
    raw: dict[str, Any] | None = None
