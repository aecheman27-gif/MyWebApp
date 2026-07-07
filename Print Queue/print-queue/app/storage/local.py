"""Local filesystem storage.

Files are saved as <uuid>.bin under the configured upload directory.
The original filename is stored in the DB row, not the on-disk name —
this avoids collisions and unsafe path characters.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import aiofiles
import aiofiles.os

from app.storage.base import FileStorage, StoredFileResult

CHUNK_SIZE = 64 * 1024


class LocalFileStorage(FileStorage):
    backend_name = "local"

    def __init__(self, upload_dir: str | Path) -> None:
        self.upload_dir = Path(upload_dir)
        self.upload_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, storage_key: str) -> Path:
        # Defense in depth: refuse traversal.
        if "/" in storage_key or ".." in storage_key or "\x00" in storage_key:
            raise ValueError(f"invalid storage_key: {storage_key!r}")
        return self.upload_dir / storage_key

    async def save(
        self,
        content: bytes,
        original_filename: str,
        mime_type: str = "application/octet-stream",
    ) -> StoredFileResult:
        storage_key = f"{uuid4().hex}.bin"
        path = self._path_for(storage_key)

        async with aiofiles.open(path, "wb") as f:
            await f.write(content)

        sha = hashlib.sha256(content).hexdigest()
        return StoredFileResult(
            storage_backend=self.backend_name,
            storage_key=storage_key,
            size_bytes=len(content),
            sha256=sha,
            mime_type=mime_type,
        )

    async def stream(self, storage_key: str) -> AsyncIterator[bytes]:
        path = self._path_for(storage_key)
        async with aiofiles.open(path, "rb") as f:
            while chunk := await f.read(CHUNK_SIZE):
                yield chunk

    async def delete(self, storage_key: str) -> None:
        path = self._path_for(storage_key)
        try:
            await aiofiles.os.remove(path)
        except FileNotFoundError:
            return
