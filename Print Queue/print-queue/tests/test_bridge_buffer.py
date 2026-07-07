"""Bridge SQLite buffer tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from bridge.buffer import TelemetryBuffer


@pytest.mark.asyncio
async def test_push_and_drain_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        b = TelemetryBuffer(str(Path(td) / "test.sqlite3"))
        await b.push({"slug": "P1", "n": 1})
        await b.push({"slug": "P1", "n": 2})
        assert await b.count() == 2
        rows = await b.drain()
        assert [r[1]["n"] for r in rows] == [1, 2]
        await b.delete([r[0] for r in rows])
        assert await b.count() == 0


@pytest.mark.asyncio
async def test_partial_delete_keeps_undeleted():
    with tempfile.TemporaryDirectory() as td:
        b = TelemetryBuffer(str(Path(td) / "test.sqlite3"))
        await b.push({"n": 1})
        await b.push({"n": 2})
        rows = await b.drain()
        await b.delete([rows[0][0]])  # only delete the first
        assert await b.count() == 1


@pytest.mark.asyncio
async def test_empty_drain_returns_empty_list():
    with tempfile.TemporaryDirectory() as td:
        b = TelemetryBuffer(str(Path(td) / "test.sqlite3"))
        assert await b.drain() == []


@pytest.mark.asyncio
async def test_persists_across_instances():
    """Same file path opened twice should see the same data."""
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "test.sqlite3")
        b1 = TelemetryBuffer(path)
        await b1.push({"hello": "world"})
        b2 = TelemetryBuffer(path)
        rows = await b2.drain()
        assert len(rows) == 1
        assert rows[0][1] == {"hello": "world"}
