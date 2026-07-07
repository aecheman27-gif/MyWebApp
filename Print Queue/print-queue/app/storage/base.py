"""File storage interface.

Implementations: LocalFileStorage (default), R2FileStorage (later, when
we wire up Cloudflare R2). Routes never touch storage internals; they go
through the FileStorage interface.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass
class StoredFileResult:
    """Returned by FileStorage.save — metadata the caller persists in the DB."""

    storage_backend: str
    storage_key: str
    size_bytes: int
    sha256: str
    mime_type: str


class FileStorage(abc.ABC):
    """Abstract file storage."""

    backend_name: str = "abstract"

    @abc.abstractmethod
    async def save(
        self,
        content: bytes,
        original_filename: str,
        mime_type: str = "application/octet-stream",
    ) -> StoredFileResult:
        """Persist file bytes; return metadata for the DB."""

    @abc.abstractmethod
    async def stream(self, storage_key: str) -> AsyncIterator[bytes]:
        """Return an async iterator yielding the file's bytes in chunks."""

    @abc.abstractmethod
    async def delete(self, storage_key: str) -> None:
        """Delete a file by its storage key. Idempotent — no error if missing."""
