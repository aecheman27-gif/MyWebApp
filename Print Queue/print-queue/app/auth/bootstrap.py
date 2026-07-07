"""Bootstrap users from `.env` into the database on startup.

The allowlist used to be enforced purely from `.env`. We now keep the
source-of-truth in the database so admins can add/remove users via the
admin UI. To avoid lockouts on a fresh deploy (or after a DB restore),
we still read `.env` once at startup and ensure every email listed there
exists as an active user with the right role.

Behavior:
- For each email in ALLOWED_EMAILS, create-or-update User to is_active=True.
- For each email in OPERATOR_EMAILS, set role to operator.
- Users NOT in `.env` are left alone (admin UI can deactivate them).

The `.env` is therefore a safety net, not the runtime allowlist. Operators
can add new users via the admin UI without restarting.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.user import User, UserRole

log = structlog.get_logger(__name__)


async def bootstrap_users_from_env(db: AsyncSession, settings: Settings) -> int:
    """Returns the number of users created or updated."""
    allowed = settings.allowed_email_set
    operators = settings.operator_email_set
    if not allowed:
        log.info("bootstrap.no_allowed_emails")
        return 0

    changes = 0
    for email in allowed:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        expected_role = UserRole.operator if email in operators else UserRole.submitter

        if user is None:
            user = User(
                email=email,
                role=expected_role,
                is_active=True,
            )
            db.add(user)
            changes += 1
            log.info("bootstrap.user_created", email=email, role=expected_role.value)
        else:
            updated = False
            if not user.is_active:
                user.is_active = True
                updated = True
            if user.role != expected_role:
                user.role = expected_role
                updated = True
            if updated:
                changes += 1
                log.info(
                    "bootstrap.user_updated",
                    email=email,
                    role=expected_role.value,
                )

    if changes:
        await db.commit()
    log.info("bootstrap.complete", changes=changes, total_allowed=len(allowed))
    return changes
