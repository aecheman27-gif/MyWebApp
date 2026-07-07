"""Admin user-management, stats, CSV, and bootstrap tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.admin import stats_service, users_service
from app.auth.bootstrap import bootstrap_users_from_env
from app.models.submission import SubmissionStatus
from app.models.user import User, UserRole

# ---------------- user management ----------------


@pytest.mark.asyncio
async def test_add_user(db_session, operator):
    u = await users_service.add_user(
        db_session, email="new@example.com", role=UserRole.submitter, actor=operator
    )
    assert u.email == "new@example.com"
    assert u.is_active is True
    assert u.role == UserRole.submitter


@pytest.mark.asyncio
async def test_add_existing_inactive_reactivates(db_session, operator):
    db_session.add(User(email="back@example.com", role=UserRole.submitter, is_active=False))
    await db_session.commit()
    u = await users_service.add_user(
        db_session, email="back@example.com", role=UserRole.operator, actor=operator
    )
    assert u.is_active is True
    assert u.role == UserRole.operator


@pytest.mark.asyncio
async def test_cannot_demote_self(db_session, operator):
    with pytest.raises(ValueError, match="yourself"):
        await users_service.set_role(
            db_session, user_id=operator.id, role=UserRole.submitter, actor=operator
        )


@pytest.mark.asyncio
async def test_cannot_deactivate_self(db_session, operator):
    with pytest.raises(ValueError, match="yourself"):
        await users_service.set_active(
            db_session, user_id=operator.id, is_active=False, actor=operator
        )


@pytest.mark.asyncio
async def test_deactivate_other_user(db_session, operator, submitter):
    u = await users_service.set_active(
        db_session, user_id=submitter.id, is_active=False, actor=operator
    )
    assert u.is_active is False


@pytest.mark.asyncio
async def test_admin_users_page_requires_operator(client, submitter_cookies):
    r = await client.get("/admin/users", cookies=submitter_cookies)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_admin_users_page_renders_for_operator(client, operator_cookies):
    r = await client.get("/admin/users", cookies=operator_cookies)
    assert r.status_code == 200
    assert "User management" in r.text


@pytest.mark.asyncio
async def test_add_user_via_http(client, operator_cookies, db_session):
    r = await client.post(
        "/admin/users",
        data={"email": "httpuser@example.com", "role": "operator"},
        cookies=operator_cookies,
        follow_redirects=False,
    )
    assert r.status_code == 303
    result = await db_session.execute(select(User).where(User.email == "httpuser@example.com"))
    assert result.scalar_one().role == UserRole.operator


# ---------------- bootstrap ----------------


@pytest.mark.asyncio
async def test_bootstrap_seeds_env_users(db_session, settings):
    # Test env ALLOWED_EMAILS = alice, bob, carol; OPERATOR_EMAILS = alice
    n = await bootstrap_users_from_env(db_session, settings)
    assert n == 3
    result = await db_session.execute(select(User))
    users = {u.email: u for u in result.scalars().all()}
    assert users["alice@example.com"].role == UserRole.operator
    assert users["bob@example.com"].role == UserRole.submitter
    assert all(u.is_active for u in users.values())


@pytest.mark.asyncio
async def test_bootstrap_reactivates_and_promotes(db_session, settings):
    db_session.add(User(email="alice@example.com", role=UserRole.submitter, is_active=False))
    await db_session.commit()
    await bootstrap_users_from_env(db_session, settings)
    result = await db_session.execute(select(User).where(User.email == "alice@example.com"))
    alice = result.scalar_one()
    assert alice.is_active is True
    assert alice.role == UserRole.operator


# ---------------- stats ----------------


@pytest.mark.asyncio
async def test_stats_counts_and_breakdowns(db_session, submitter, operator, settings):
    from app.storage import get_storage
    from app.submissions import service as sub_service
    from app.submissions.schemas import SubmissionCreate

    storage = get_storage()
    for i in range(3):
        await sub_service.create_submission(
            db_session,
            settings,
            storage,
            submitter,
            SubmissionCreate(part_name=f"part-{i}"),
            None,
            None,
        )

    start = datetime.now(UTC) - timedelta(days=1)
    end = datetime.now(UTC) + timedelta(days=1)
    stats = await stats_service.compute_stats(db_session, range_start=start, range_end=end)
    assert stats.submissions_total == 3
    assert stats.by_submitter[submitter.email] == 3


@pytest.mark.asyncio
async def test_stats_print_duration_from_events(db_session, submitter, operator, settings):
    from app.models.submission_event import EventType, SubmissionEvent
    from app.storage import get_storage
    from app.submissions import service as sub_service
    from app.submissions.schemas import SubmissionCreate

    storage = get_storage()
    sub = await sub_service.create_submission(
        db_session,
        settings,
        storage,
        submitter,
        SubmissionCreate(part_name="timed"),
        None,
        None,
    )

    base = datetime.now(UTC)
    db_session.add(
        SubmissionEvent(
            submission_id=sub.id,
            event_type=EventType.STATUS_CHANGED,
            to_status=SubmissionStatus.PRINTING,
            created_at=base,
        )
    )
    db_session.add(
        SubmissionEvent(
            submission_id=sub.id,
            event_type=EventType.STATUS_CHANGED,
            from_status=SubmissionStatus.PRINTING,
            to_status=SubmissionStatus.DONE,
            created_at=base + timedelta(minutes=90),
        )
    )
    sub.status = SubmissionStatus.DONE
    await db_session.commit()

    start = base - timedelta(days=1)
    end = base + timedelta(days=1)
    stats = await stats_service.compute_stats(db_session, range_start=start, range_end=end)
    assert stats.total_print_minutes == 90
    assert stats.longest_print_minutes == 90


@pytest.mark.asyncio
async def test_csv_export(db_session, submitter, settings):
    from app.storage import get_storage
    from app.submissions import service as sub_service
    from app.submissions.schemas import SubmissionCreate

    storage = get_storage()
    await sub_service.create_submission(
        db_session,
        settings,
        storage,
        submitter,
        SubmissionCreate(part_name="csvpart"),
        None,
        None,
    )
    start = datetime.now(UTC) - timedelta(days=1)
    end = datetime.now(UTC) + timedelta(days=1)
    csv_text = await stats_service.csv_export_submissions(db_session, start, end)
    assert "csvpart" in csv_text
    assert "submitter_email" in csv_text  # header present


@pytest.mark.asyncio
async def test_stats_page_requires_operator(client, submitter_cookies):
    r = await client.get("/admin/stats", cookies=submitter_cookies)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_format_minutes():
    assert stats_service.format_minutes(45) == "45m"
    assert stats_service.format_minutes(90) == "1h 30m"
    assert stats_service.format_minutes(120) == "2h 0m"
