"""Comment thread tests."""

from __future__ import annotations

import pytest

from app.comments import service as comment_service
from app.storage import get_storage
from app.submissions import service as sub_service
from app.submissions.schemas import SubmissionCreate


async def _make_sub(db_session, settings, submitter, part="bracket"):
    return await sub_service.create_submission(
        db_session,
        settings,
        get_storage(),
        submitter,
        SubmissionCreate(part_name=part),
        None,
        None,
    )


@pytest.mark.asyncio
async def test_create_and_list_comment(db_session, submitter, settings, monkeypatch):
    # Silence notifications for this test.
    async def noop(*a, **k):
        pass

    monkeypatch.setattr(comment_service, "notify_comment_added", noop)

    sub = await _make_sub(db_session, settings, submitter)
    c = await comment_service.create(
        db_session, settings, submission=sub, author=submitter, body="Looks good"
    )
    assert c.body == "Looks good"
    assert c.author_email_at_write == submitter.email

    comments = await comment_service.list_for_submission(db_session, sub.id)
    assert len(comments) == 1
    assert comments[0].body == "Looks good"


@pytest.mark.asyncio
async def test_empty_comment_rejected(db_session, submitter, settings, monkeypatch):
    async def noop(*a, **k):
        pass

    monkeypatch.setattr(comment_service, "notify_comment_added", noop)

    sub = await _make_sub(db_session, settings, submitter)
    with pytest.raises(ValueError, match="empty"):
        await comment_service.create(
            db_session, settings, submission=sub, author=submitter, body="   "
        )


@pytest.mark.asyncio
async def test_overlong_comment_rejected(db_session, submitter, settings, monkeypatch):
    async def noop(*a, **k):
        pass

    monkeypatch.setattr(comment_service, "notify_comment_added", noop)

    sub = await _make_sub(db_session, settings, submitter)
    with pytest.raises(ValueError, match="exceeds"):
        await comment_service.create(
            db_session,
            settings,
            submission=sub,
            author=submitter,
            body="x" * 2001,
        )


@pytest.mark.asyncio
async def test_operator_comment_notifies_submitter(
    db_session, submitter, operator, settings, monkeypatch
):
    notified: list[dict] = []

    async def fake_comment_email(settings, **kwargs):
        notified.append(kwargs)

    from app.notifications import service as notify_service

    monkeypatch.setattr(notify_service, "send_comment_email", fake_comment_email)

    sub = await _make_sub(db_session, settings, submitter)
    await comment_service.create(
        db_session, settings, submission=sub, author=operator, body="Reslicing this"
    )
    # Submitter (bob) should be notified; operator (author) should not.
    assert len(notified) == 1
    assert notified[0]["to_email"] == submitter.email


@pytest.mark.asyncio
async def test_submitter_comment_notifies_operators(
    db_session, submitter, operator, settings, monkeypatch
):
    notified: list[dict] = []

    async def fake_comment_email(settings, **kwargs):
        notified.append(kwargs)

    from app.notifications import service as notify_service

    monkeypatch.setattr(notify_service, "send_comment_email", fake_comment_email)

    sub = await _make_sub(db_session, settings, submitter)
    await comment_service.create(
        db_session, settings, submission=sub, author=submitter, body="Any update?"
    )
    # Operator (alice) should be notified.
    assert any(n["to_email"] == operator.email for n in notified)


@pytest.mark.asyncio
async def test_comment_via_http(
    client, submitter, submitter_cookies, db_session, settings, monkeypatch
):
    from app.comments import service as cs

    async def noop(*a, **k):
        pass

    monkeypatch.setattr(cs, "notify_comment_added", noop)

    sub = await _make_sub(db_session, settings, submitter)
    r = await client.post(
        f"/submissions/{sub.id}/comments",
        data={"body": "via http"},
        cookies=submitter_cookies,
        follow_redirects=False,
    )
    assert r.status_code == 303
    comments = await cs.list_for_submission(db_session, sub.id)
    assert len(comments) == 1
