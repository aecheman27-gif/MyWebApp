"""Local SQLite buffer for telemetry events we couldn't deliver yet.

The app server should normally be reachable from the bridge over the
internal Docker network, so this buffer mainly handles the case where
the web container is restarting or briefly unhealthy. On reconnect we
drain in FIFO order.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path


class TelemetryBuffer:
    def __init__(self, db_path: str) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._setup()

    def _setup(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS buffered_telemetry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """)

    async def push(self, payload: dict) -> None:
        async with self._lock:
            await asyncio.to_thread(self._push_sync, payload)

    def _push_sync(self, payload: dict) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT INTO buffered_telemetry (payload_json) VALUES (?)",
                (json.dumps(payload),),
            )

    async def drain(self) -> list[tuple[int, dict]]:
        async with self._lock:
            return await asyncio.to_thread(self._drain_sync)

    def _drain_sync(self) -> list[tuple[int, dict]]:
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute(
                "SELECT id, payload_json FROM buffered_telemetry ORDER BY id ASC LIMIT 500"
            )
            return [(row[0], json.loads(row[1])) for row in cur.fetchall()]

    async def delete(self, ids: Iterable[int]) -> None:
        ids = list(ids)
        if not ids:
            return
        async with self._lock:
            await asyncio.to_thread(self._delete_sync, ids)

    def _delete_sync(self, ids: list[int]) -> None:
        with sqlite3.connect(self.path) as conn:
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"DELETE FROM buffered_telemetry WHERE id IN ({placeholders})",
                ids,
            )

    async def count(self) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._count_sync)

    def _count_sync(self) -> int:
        with sqlite3.connect(self.path) as conn:
            return conn.execute("SELECT COUNT(*) FROM buffered_telemetry").fetchone()[0]
