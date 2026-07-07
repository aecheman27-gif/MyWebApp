"""Magic-link token generation and verification.

Tokens are 32-byte URL-safe random strings. We store the SHA-256 hash
(64 hex chars) in the DB and ship the raw value in the email. The raw
value never appears in our logs.
"""

from __future__ import annotations

import hashlib
import secrets


def generate_token() -> str:
    """Return a fresh URL-safe magic-link token."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Return the hex SHA-256 of a token. Used for DB storage and lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
