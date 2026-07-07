"""Submission lifecycle: create, edit, status, delete, queue queries."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.auth.service import SESSION_COOKIE_NAME, create_session_cookie
from app.config import get_settings
from app.models.submission import (
    SubmissionPriority,
    SubmissionStatus,
)
from app.models.submission_event import EventType, SubmissionEvent
from app.storage import get_storage
from app.submissions import service
from app.submissions.schemas import SubmissionCreate, SubmissionEdit


@pytest.mark.asyncio
async def test_create_submission_without_file(db_session, submitter, settings):
    storage = get_storage()
    data = SubmissionCreate(part_name="Test bracket", priority=SubmissionPriority.HIGH)
    sub = await service.create_submission(
        db_session, settings, storage, submitter, data, None, None
    )

    assert sub.id is not None
    assert sub.submitter_id == submitter.id
    assert sub.part_name == "Test bracket"
    assert sub.priority == SubmissionPriority.HIGH
    assert sub.status == SubmissionStatus.QUEUED
    assert sub.file_id is None

    # CREATED audit row present
    events = (await db_session.execute(select(SubmissionEvent))).scalars().all()
    assert len(events) == 1
    assert events[0].event_type == EventType.CREATED


@pytest.mark.asyncio
async def test_create_submission_with_file(db_session, submitter, settings):
    storage = get_storage()
    data = SubmissionCreate(part_name="Bracket", description="Test desc")
    content = b"solid Bracket\nendsolid\n" * 50
    sub = await service.create_submission(
        db_session, settings, storage, submitter, data, content, "bracket.stl", "model/stl"
    )
    assert sub.file_id is not None
    await db_session.refresh(sub)
    assert sub.file is not None
    assert sub.file.original_filename == "bracket.stl"
    assert sub.file.size_bytes == len(content)


@pytest.mark.asyncio
async def test_create_rejects_oversize_file(db_session, submitter, settings):
    # MAX_UPLOAD_MB=10 in test env, so 11 MB should fail
    storage = get_storage()
    data = SubmissionCreate(part_name="x")
    huge = b"x" * (11 * 1024 * 1024)
    with pytest.raises(Exception) as exc_info:
        await service.create_submission(
            db_session, settings, storage, submitter, data, huge, "x.stl", "model/stl"
        )
    assert "413" in str(exc_info.value) or "exceeds" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_create_rejects_wrong_extension(db_session, submitter, settings):
    storage = get_storage()
    data = SubmissionCreate(part_name="x")
    with pytest.raises(Exception) as exc_info:
        await service.create_submission(
            db_session,
            settings,
            storage,
            submitter,
            data,
            b"data",
            "evil.exe",
            "application/octet-stream",
        )
    assert "step" in str(exc_info.value).lower() or "400" in str(exc_info.value)


@pytest.mark.asyncio
async def test_edit_submission(db_session, submitter, settings):
    storage = get_storage()
    sub = await service.create_submission(
        db_session,
        settings,
        storage,
        submitter,
        SubmissionCreate(part_name="Original"),
        None,
        None,
    )
    edited = await service.edit_submission(
        db_session,
        sub,
        submitter,
        SubmissionEdit(part_name="Updated", priority=SubmissionPriority.RUSH),
    )
    assert edited.part_name == "Updated"
    assert edited.priority == SubmissionPriority.RUSH
    events = (await db_session.execute(select(SubmissionEvent))).scalars().all()
    types = [e.event_type for e in events]
    assert EventType.EDITED in types


@pytest.mark.asyncio
async def test_status_change(db_session, operator, submitter, settings):
    storage = get_storage()
    sub = await service.create_submission(
        db_session,
        settings,
        storage,
        submitter,
        SubmissionCreate(part_name="x"),
        None,
        None,
    )
    updated = await service.change_status(db_session, sub, operator, SubmissionStatus.PRINTING)
    assert updated.status == SubmissionStatus.PRINTING
    events = (await db_session.execute(select(SubmissionEvent))).scalars().all()
    sc_event = next(e for e in events if e.event_type == EventType.STATUS_CHANGED)
    assert sc_event.from_status == SubmissionStatus.QUEUED
    assert sc_event.to_status == SubmissionStatus.PRINTING


@pytest.mark.asyncio
async def test_status_change_to_same_status_is_noop(db_session, operator, submitter, settings):
    storage = get_storage()
    sub = await service.create_submission(
        db_session,
        settings,
        storage,
        submitter,
        SubmissionCreate(part_name="x"),
        None,
        None,
    )
    await service.change_status(db_session, sub, operator, SubmissionStatus.QUEUED)
    events = (await db_session.execute(select(SubmissionEvent))).scalars().all()
    assert not any(e.event_type == EventType.STATUS_CHANGED for e in events)


@pytest.mark.asyncio
async def test_delete_submission(db_session, operator, submitter, settings):
    storage = get_storage()
    sub = await service.create_submission(
        db_session,
        settings,
        storage,
        submitter,
        SubmissionCreate(part_name="x"),
        b"solid x\nendsolid\n",
        "x.stl",
        "model/stl",
    )
    sub_id = sub.id
    await service.delete_submission(db_session, storage, sub, operator)
    found = await service.get_submission(db_session, sub_id)
    assert found is None


@pytest.mark.asyncio
async def test_list_filters(db_session, submitter, settings):
    storage = get_storage()
    for name, status_ in [
        ("a", SubmissionStatus.QUEUED),
        ("b", SubmissionStatus.PRINTING),
        ("c", SubmissionStatus.DONE),
    ]:
        sub = await service.create_submission(
            db_session,
            settings,
            storage,
            submitter,
            SubmissionCreate(part_name=name),
            None,
            None,
        )
        sub.status = status_
    await db_session.commit()

    active = await service.list_submissions(db_session, filter_="active")
    assert {s.part_name for s in active} == {"a", "b"}

    in_progress = await service.list_submissions(db_session, filter_="in_progress")
    assert {s.part_name for s in in_progress} == {"b"}

    completed = await service.list_submissions(db_session, filter_="completed")
    assert {s.part_name for s in completed} == {"c"}


@pytest.mark.asyncio
async def test_list_priority_ordering(db_session, submitter, settings):
    storage = get_storage()
    for name, prio in [
        ("low", SubmissionPriority.LOW),
        ("rush", SubmissionPriority.RUSH),
        ("normal", SubmissionPriority.NORMAL),
        ("high", SubmissionPriority.HIGH),
    ]:
        await service.create_submission(
            db_session,
            settings,
            storage,
            submitter,
            SubmissionCreate(part_name=name, priority=prio),
            None,
            None,
        )
    items = await service.list_submissions(db_session, filter_="active")
    assert [s.part_name for s in items] == ["rush", "high", "normal", "low"]


@pytest.mark.asyncio
async def test_search(db_session, submitter, settings):
    storage = get_storage()
    await service.create_submission(
        db_session,
        settings,
        storage,
        submitter,
        SubmissionCreate(part_name="Cable bracket"),
        None,
        None,
    )
    await service.create_submission(
        db_session,
        settings,
        storage,
        submitter,
        SubmissionCreate(part_name="Fixture mount"),
        None,
        None,
    )
    found = await service.list_submissions(db_session, filter_="all", search="bracket")
    assert len(found) == 1
    assert found[0].part_name == "Cable bracket"


@pytest.mark.asyncio
async def test_filename_hint_is_stable_and_safe(db_session, submitter, settings):
    storage = get_storage()
    sub = await service.create_submission(
        db_session,
        settings,
        storage,
        submitter,
        SubmissionCreate(part_name="My Cool/Bracket v2!"),
        None,
        None,
    )
    hint = sub.filename_hint
    assert hint.startswith("sub-")
    assert hint.endswith(".3mf")
    assert "/" not in hint
    assert "!" not in hint


# === HTTP-level tests ===


def _cookie_for(user):
    settings = get_settings()
    return {SESSION_COOKIE_NAME: create_session_cookie(settings, user.id)}


@pytest.mark.asyncio
async def test_queue_page_requires_auth(client):
    r = await client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_queue_page_renders_for_user(client, submitter):
    r = await client.get("/", cookies=_cookie_for(submitter))
    assert r.status_code == 200
    assert "Print Queue" in r.text


@pytest.mark.asyncio
async def test_create_via_http(client, submitter):
    files = {"file": ("test.stl", b"solid x\nendsolid\n", "model/stl")}
    data = {"part_name": "From HTTP", "material": "PLA", "priority": "NORMAL"}
    r = await client.post(
        "/submissions",
        data=data,
        files=files,
        cookies=_cookie_for(submitter),
        follow_redirects=False,
    )
    assert r.status_code == 303


@pytest.mark.asyncio
async def test_submitter_cannot_change_status_via_http(client, submitter, db_session, settings):
    storage = get_storage()
    sub = await service.create_submission(
        db_session,
        settings,
        storage,
        submitter,
        SubmissionCreate(part_name="x"),
        None,
        None,
    )
    r = await client.post(
        f"/submissions/{sub.id}/status",
        data={"to_status": "PRINTING"},
        cookies=_cookie_for(submitter),
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_operator_can_change_status_via_http(
    client, operator, submitter, db_session, settings
):
    storage = get_storage()
    sub = await service.create_submission(
        db_session,
        settings,
        storage,
        submitter,
        SubmissionCreate(part_name="x"),
        None,
        None,
    )
    r = await client.post(
        f"/submissions/{sub.id}/status",
        data={"to_status": "PRINTING"},
        cookies=_cookie_for(operator),
        follow_redirects=False,
    )
    assert r.status_code == 303
