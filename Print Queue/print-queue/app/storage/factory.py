"""Storage backend selection based on settings."""

from __future__ import annotations

from functools import lru_cache

from app.config import Settings, get_settings
from app.storage.base import FileStorage
from app.storage.local import LocalFileStorage


@lru_cache
def get_storage() -> FileStorage:
    settings = get_settings()
    return _build_storage(settings)


def _build_storage(settings: Settings) -> FileStorage:
    backend = settings.storage_backend.lower()
    if backend == "local":
        return LocalFileStorage(upload_dir=settings.upload_dir)
    # Room here for R2FileStorage etc.
    raise ValueError(f"Unknown STORAGE_BACKEND: {settings.storage_backend!r}")


def reset_storage_cache() -> None:
    """Used by tests to swap out the cached storage instance."""
    get_storage.cache_clear()
