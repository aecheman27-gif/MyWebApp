"""User-management service used by /admin/users."""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserRole

log = structlog.get_logger(__name__)


async def list_users(db: AsyncSession) -> list[User]:
    result = await db.execute(select(User).order_by(User.is_active.desc(), User.email.asc()))
    return list(result.scalars().all())


async def add_user(db: AsyncSession, *, email: str, role: UserRole, actor: User) -> User:
    email = email.strip().lower()
    if not email or "@" not in email:
        raise ValueError("Invalid email address")

    result = await db.execute(select(User).where(User.email == email))
    existing = result.scalar_one_or_none()
    if existing is not None:
        if not existing.is_active:
            existing.is_active = True
            existing.role = role
            await db.commit()
            log.info("admin.user.reactivated", email=email, by=actor.email)
            return existing
        if existing.role != role:
            existing.role = role
            await db.commit()
            log.info("admin.user.role_updated", email=email, role=role.value, by=actor.email)
        return existing

    user = User(email=email, role=role, is_active=True)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    log.info("admin.user.created", email=email, role=role.value, by=actor.email)
    return user


async def set_role(db: AsyncSession, *, user_id: UUID, role: UserRole, actor: User) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise ValueError("User not found")
    if user.id == actor.id and role != UserRole.operator:
        raise ValueError("Refusing to demote yourself (would lock you out of admin)")
    user.role = role
    await db.commit()
    log.info("admin.user.role_changed", email=user.email, role=role.value, by=actor.email)
    return user


async def set_active(db: AsyncSession, *, user_id: UUID, is_active: bool, actor: User) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise ValueError("User not found")
    if user.id == actor.id and not is_active:
        raise ValueError("Refusing to deactivate yourself")
    user.is_active = is_active
    await db.commit()
    log.info(
        "admin.user.active_changed",
        email=user.email,
        is_active=is_active,
        by=actor.email,
    )
    return user
