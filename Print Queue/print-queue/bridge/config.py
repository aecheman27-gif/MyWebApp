"""Bridge configuration.

Configured via env vars. Multiple printers are declared by repeating
PRINTER_<N>_* groups (1, 2, ...).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PrinterConfig:
    slug: str
    name: str
    host: str
    port: int
    serial: str
    access_code: str  # 8-digit code from the printer's network settings


@dataclass(frozen=True)
class BridgeConfig:
    app_telemetry_url: str
    bridge_shared_token: str
    buffer_db_path: str
    printers: tuple[PrinterConfig, ...]
    poll_request_interval: int  # seconds between explicit pushall refreshes
    log_level: str

    @classmethod
    def from_env(cls) -> BridgeConfig:
        printers = []
        for i in range(1, 11):  # support up to 10 printers
            slug = os.environ.get(f"PRINTER_{i}_SLUG")
            if not slug:
                continue
            host = os.environ.get(f"PRINTER_{i}_HOST")
            serial = os.environ.get(f"PRINTER_{i}_SERIAL")
            access_code = os.environ.get(f"PRINTER_{i}_ACCESS_CODE")
            if not (host and serial and access_code):
                raise RuntimeError(
                    f"PRINTER_{i}_SLUG set but missing one of "
                    f"PRINTER_{i}_HOST / PRINTER_{i}_SERIAL / PRINTER_{i}_ACCESS_CODE"
                )
            printers.append(
                PrinterConfig(
                    slug=slug.strip(),
                    name=os.environ.get(f"PRINTER_{i}_NAME", slug).strip(),
                    host=host.strip(),
                    port=int(os.environ.get(f"PRINTER_{i}_PORT", "8883")),
                    serial=serial.strip(),
                    access_code=access_code.strip(),
                )
            )
        if not printers:
            raise RuntimeError("No printers configured (set PRINTER_1_SLUG etc.)")

        return cls(
            app_telemetry_url=os.environ.get(
                "APP_TELEMETRY_URL", "http://web:8000/internal/telemetry"
            ),
            bridge_shared_token=os.environ.get("BRIDGE_SHARED_TOKEN", ""),
            buffer_db_path=os.environ.get("BRIDGE_BUFFER_PATH", "/data/buffer.sqlite3"),
            printers=tuple(printers),
            poll_request_interval=int(os.environ.get("BRIDGE_PUSHALL_INTERVAL", "300")),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )
