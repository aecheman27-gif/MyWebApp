"""One MQTT connection per Bambu printer.

Connects to the printer's local broker over TLS (self-signed cert; verify
disabled per Bambu's documented protocol). Subscribes to the report
topic, publishes a `pushall` on connect, and forwards each report dict
through the on_telemetry callback.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import Awaitable, Callable
from typing import Any

import paho.mqtt.client as mqtt
import structlog

from bridge.config import PrinterConfig
from bridge.parser import PrinterAccumulator

log = structlog.get_logger(__name__)

OnSnapshot = Callable[[dict[str, Any]], Awaitable[None]]


class PrinterConnection:
    def __init__(
        self,
        cfg: PrinterConfig,
        on_snapshot: OnSnapshot,
        loop: asyncio.AbstractEventLoop,
    ):
        self.cfg = cfg
        self.on_snapshot = on_snapshot
        self.loop = loop
        self.accumulator = PrinterAccumulator(slug=cfg.slug, serial=cfg.serial)
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"printq-bridge-{cfg.slug}",
        )
        self._client.username_pw_set("bblp", cfg.access_code)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self._client.tls_set_context(ctx)
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._connected = asyncio.Event()
        self._closed = False

    @property
    def report_topic(self) -> str:
        return f"device/{self.cfg.serial}/report"

    @property
    def request_topic(self) -> str:
        return f"device/{self.cfg.serial}/request"

    def start(self) -> None:
        log.info(
            "printer.connecting",
            slug=self.cfg.slug,
            host=self.cfg.host,
            port=self.cfg.port,
        )
        try:
            self._client.connect_async(self.cfg.host, self.cfg.port, keepalive=60)
        except OSError as e:
            log.warning("printer.initial_connect_failed", slug=self.cfg.slug, error=str(e))
        self._client.loop_start()

    def request_pushall(self) -> None:
        """Ask the printer for a full state snapshot."""
        if not self._connected.is_set():
            return
        try:
            self._client.publish(
                self.request_topic,
                json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}),
                qos=0,
            )
        except Exception as e:
            log.warning("printer.pushall_failed", slug=self.cfg.slug, error=str(e))

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None):
        if reason_code == 0 or (hasattr(reason_code, "value") and reason_code.value == 0):
            log.info("printer.connected", slug=self.cfg.slug)
            self._connected.set()
            client.subscribe(self.report_topic, qos=0)
            self.request_pushall()
        else:
            log.warning("printer.connect_refused", slug=self.cfg.slug, reason=str(reason_code))

    def _on_disconnect(self, _client, _userdata, _flags, reason_code, _properties=None):
        log.info("printer.disconnected", slug=self.cfg.slug, reason=str(reason_code))
        self._connected.clear()

    def _on_message(self, _client, _userdata, msg):
        # paho callbacks run on its IO thread; bounce into asyncio loop.
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return

        # Bambu wraps everything under "print" most of the time.
        print_section = payload.get("print") if isinstance(payload, dict) else None
        if not isinstance(print_section, dict):
            return

        self.accumulator.apply(print_section)
        snapshot = self.accumulator.snapshot()
        asyncio.run_coroutine_threadsafe(self.on_snapshot(snapshot), self.loop)

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()
