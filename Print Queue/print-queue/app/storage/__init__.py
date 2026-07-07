"""Pluggable file storage.

LocalFileStorage saves files to disk; R2FileStorage (future) would push
to Cloudflare R2. Configured via STORAGE_BACKEND env var.
"""

from app.storage.base import FileStorage, StoredFileResult
from app.storage.factory import get_storage
from app.storage.local import LocalFileStorage

__all__ = ["FileStorage", "LocalFileStorage", "StoredFileResult", "get_storage"]
