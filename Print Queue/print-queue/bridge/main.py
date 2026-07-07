"""Bridge service entry point.

Wires up logging, builds the AppClient, opens one PrinterConnection per
configured printer, and runs forever. Two loops:

- on_snapshot (fired by each MQTT report): post to app; on failure, buffer.
- drain_loop (every few seconds): try to drain the buffer.
- pushall_loop (every BRIDGE_PUSHALL_INTERVAL seconds): ask each printer
  for a full state refresh, defensive against missed incremental updates.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import structlog

from bridge.app_client import AppClient
from bridge.buffer import TelemetryBuffer
from bridge.config import BridgeConfig
from bridge.printer import PrinterConnection


def _configure_logging(level: str) -> None:
    logging.basicConfig(level=level, stream=sys.stdout, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


async def run(config: BridgeConfig) -> None:
    log = structlog.get_logger("bridge")
    log.info(
        "bridge.start",
        printer_count=len(config.printers),
        telemetry_url=config.app_telemetry_url,
    )

    buffer = TelemetryBuffer(config.buffer_db_path)
    app_client = AppClient(config.app_telemetry_url, config.bridge_shared_token)
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    async def on_snapshot(payload: dict) -> None:
        ok = await app_client.post(payload)
        if not ok:
            await buffer.push(payload)

    connections = [PrinterConnection(p, on_snapshot, loop) for p in config.printers]
    for c in connections:
        c.start()

    async def drain_loop():
        while not stop_event.is_set():
            try:
                rows = await buffer.drain()
                if rows:
                    drained_ids = []
                    for row_id, payload in rows:
                        if await app_client.post(payload):
                            drained_ids.append(row_id)
                        else:
                            break  # leave the rest for later
                    if drained_ids:
                        await buffer.delete(drained_ids)
                        log.info("buffer.drained", count=len(drained_ids))
            except Exception as e:
                log.warning("drain.error", error=str(e))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5.0)
            except TimeoutError:
                pass

    async def pushall_loop():
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=config.poll_request_interval)
                break  # stop_event fired
            except TimeoutError:
                for c in connections:
                    c.request_pushall()

    def _signal_handler():
        log.info("bridge.signal_received")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    drain_task = asyncio.create_task(drain_loop())
    pushall_task = asyncio.create_task(pushall_loop())
    await stop_event.wait()

    log.info("bridge.shutting_down")
    drain_task.cancel()
    pushall_task.cancel()
    for c in connections:
        await c.stop()
    await app_client.close()
    log.info("bridge.stopped")


def main() -> None:
    config = BridgeConfig.from_env()
    _configure_logging(config.log_level)
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
