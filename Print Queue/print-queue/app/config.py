"""Application configuration, sourced from environment variables.

Single source of truth for every configurable value. All other modules
import Settings from here rather than reading os.environ directly.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Site
    site_url: str = "http://localhost:8000"
    site_name: str = "Print Queue"
    env: str = "development"
    log_level: str = "INFO"

    # Secrets
    session_secret: str = Field(
        default="dev-only-secret-change-in-production-please-please-please",
        description="Secret used to sign session cookies. Must be 32+ chars in prod.",
    )

    # Database
    database_url: str = "postgresql+asyncpg://printq:printq@localhost:5432/printq"

    # Auth allowlists (comma-separated emails)
    allowed_emails: str = ""
    operator_emails: str = ""

    # Resend
    resend_api_key: str = ""
    email_from: str = "Print Queue <noreply@example.com>"

    # Sentry
    sentry_dsn: str = ""

    # Magic-link token TTL
    magic_link_ttl_minutes: int = 15
    # Session cookie TTL
    session_ttl_days: int = 30

    # File storage
    storage_backend: str = "local"
    upload_dir: str = "/data/uploads"
    max_upload_mb: int = 100
    file_retention_days: int = 90

    # Bridge / telemetry
    bridge_shared_token: str = ""
    printer_stale_seconds: int = 30

    # Notifications: optional outbound webhooks for print failures.
    # Set either or both; if empty, that channel is disabled.
    slack_webhook_url: str = ""
    discord_webhook_url: str = ""

    @property
    def allowed_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.allowed_emails.split(",") if e.strip()}

    @property
    def operator_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.operator_emails.split(",") if e.strip()}

    def is_email_allowed(self, email: str) -> bool:
        return email.lower() in self.allowed_email_set

    def is_operator(self, email: str) -> bool:
        return email.lower() in self.operator_email_set

    @property
    def is_production(self) -> bool:
        return self.env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
