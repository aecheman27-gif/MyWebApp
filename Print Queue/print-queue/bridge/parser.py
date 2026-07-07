"""Normalize the Bambu MQTT `print` report into our TelemetryEvent.

The Bambu reports a partial `print` dict on each update. We accumulate
the most recent value for each field across reports (the report stream
is incremental — fields that don't change aren't always re-sent).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Map of Bambu gcode_state -> our PrinterStatus value (string, to avoid
# a hard import dependency from the bridge into the app's model code).
_GCODE_TO_STATUS = {
    "IDLE": "IDLE",
    "PREPARE": "PREPARING",
    "RUNNING": "PRINTING",
    "PAUSE": "PAUSED",
    "FINISH": "FINISHED",
    "FAILED": "FAILED",
}


@dataclass
class PrinterAccumulator:
    """Holds the latest known values for one printer across MQTT messages.

    We expose a `snapshot()` that converts to the TelemetryEvent payload
    format (a plain dict) the bridge will POST to the app.
    """

    slug: str
    serial: str

    gcode_state: str | None = None
    subtask_name: str | None = None
    mc_percent: float | None = None
    mc_remaining_time: int | None = None
    layer_num: int | None = None
    total_layer_num: int | None = None
    nozzle_temper: float | None = None
    nozzle_target_temper: float | None = None
    bed_temper: float | None = None
    bed_target_temper: float | None = None
    wifi_signal: int | None = None
    print_error: int | None = None
    raw_last: dict[str, Any] = field(default_factory=dict)

    def apply(self, report: dict[str, Any]) -> None:
        """Merge a `print` report dict from Bambu into our accumulator."""
        if not isinstance(report, dict):
            return
        self.raw_last = report

        def take(key: str, attr: str | None = None, cast=lambda x: x):
            attr = attr or key
            if key in report and report[key] is not None:
                try:
                    setattr(self, attr, cast(report[key]))
                except (ValueError, TypeError):
                    pass

        take("gcode_state")
        take("subtask_name")
        take("mc_percent", cast=float)
        take("mc_remaining_time", cast=int)
        take("layer_num", cast=int)
        take("total_layer_num", cast=int)
        take("nozzle_temper", cast=float)
        take("nozzle_target_temper", cast=float)
        take("bed_temper", cast=float)
        take("bed_target_temper", cast=float)
        # wifi_signal often arrives like "-52dBm" — strip non-numeric.
        if "wifi_signal" in report and report["wifi_signal"] is not None:
            v = str(report["wifi_signal"])
            digits = "".join(c for c in v if c == "-" or c.isdigit())
            try:
                self.wifi_signal = int(digits) if digits else None
            except ValueError:
                pass
        take("print_error", cast=int)

    def snapshot(self) -> dict[str, Any]:
        """Build the JSON payload to POST to the app."""
        status = _GCODE_TO_STATUS.get(self.gcode_state or "") or "IDLE"
        return {
            "printer_slug": self.slug,
            "printer_serial": self.serial,
            "ts": datetime.now(UTC).isoformat(),
            "status": status,
            "current_file": self.subtask_name,
            "percent": self.mc_percent,
            "remaining_minutes": self.mc_remaining_time,
            "layer": self.layer_num,
            "total_layers": self.total_layer_num,
            "nozzle_temp": self.nozzle_temper,
            "nozzle_target": self.nozzle_target_temper,
            "bed_temp": self.bed_temper,
            "bed_target": self.bed_target_temper,
            "wifi_signal": self.wifi_signal,
            "error_code": self.print_error,
            "raw": self.raw_last,
        }
