"""In-process pub/sub for streaming telemetry to SSE subscribers.

Each browser tab that opens the queue page subscribes to one channel
(broadcast — they all see the same printer state). When the bridge POSTs
new telemetry, we fan it out to every subscriber.

This is a single-process design — fine for our scale (one app container,
a handful of users). If we ever run multiple app instances, swap for
Redis pub/sub.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import structlog

log = structlog.get_logger(__name__)


class TelemetryBroadcaster:
    def __init__(self, max_queue_per_subscriber: int = 100) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._max_q = max_queue_per_subscriber
        self._lock = asyncio.Lock()

    async def publish(self, payload: dict) -> None:
        """Fan out a payload to every active subscriber. Slow subscribers
        get dropped events rather than blocking the publisher."""
        line = json.dumps(payload, default=str)
        async with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            if q.qsize() >= self._max_q:
                # Drop oldest to make room — keeps the stream fresh.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(line)

    async def subscribe(self) -> AsyncIterator[str]:
        """Yields raw JSON strings, one per telemetry event."""
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=self._max_q)
        async with self._lock:
            self._subscribers.add(q)
        log.info("telemetry.subscriber_added", count=len(self._subscribers))
        try:
            while True:
                yield await q.get()
        finally:
            async with self._lock:
                self._subscribers.discard(q)
            log.info("telemetry.subscriber_removed", count=len(self._subscribers))

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


# Module-level singleton — one broadcaster per app process.
broadcaster = TelemetryBroadcaster()
