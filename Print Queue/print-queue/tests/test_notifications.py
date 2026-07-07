"""Notification dispatch tests.

We don't hit real Resend/Slack/Discord — we monkeypatch the leaf send
functions and assert they're called (or not) under the right conditions.
"""

from __future__ import annotations

import pytest

from app.models.submission import SubmissionStatus
from app.notifications import service as notify_service


@pytest.mark.asyncio
async def test_status_change_emails_submitter(db_session, submitter, settings, monkeypatch):
    from app.storage import get_storage
    from app.submissions import service as sub_service
    from app.submissions.schemas import SubmissionCreate

    sub = await sub_service.create_submission(
        db_session,
        settings,
        get_storage(),
        submitter,
        SubmissionCreate(part_name="bracket"),
        None,
        None,
    )

    sent: list[dict] = []

    async def fake_email(settings, **kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(notify_service, "send_status_change_email", fake_email)

    await notify_service.notify_status_changed(
        db_session,
        settings,
        submission=sub,
        from_status=SubmissionStatus.QUEUED,
        to_status=SubmissionStatus.DONE,
    )
    assert len(sent) == 1
    assert sent[0]["to_email"] == submitter.email
    assert sent[0]["new_status"] == "DONE"


@pytest.mark.asyncio
async def test_status_change_respects_opt_out(db_session, submitter, settings, monkeypatch):
    from app.storage import get_storage
    from app.submissions import service as sub_service
    from app.submissions.schemas import SubmissionCreate

    submitter.email_notifications = False
    await db_session.commit()

    sub = await sub_service.create_submission(
        db_session,
        settings,
        get_storage(),
        submitter,
        SubmissionCreate(part_name="bracket"),
        None,
        None,
    )

    sent: list[dict] = []

    async def fake_email(settings, **kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(notify_service, "send_status_change_email", fake_email)

    await notify_service.notify_status_changed(
        db_session,
        settings,
        submission=sub,
        from_status=SubmissionStatus.QUEUED,
        to_status=SubmissionStatus.DONE,
    )
    assert sent == []


@pytest.mark.asyncio
async def test_no_email_for_uninteresting_status(db_session, submitter, settings, monkeypatch):
    from app.storage import get_storage
    from app.submissions import service as sub_service
    from app.submissions.schemas import SubmissionCreate

    sub = await sub_service.create_submission(
        db_session,
        settings,
        get_storage(),
        submitter,
        SubmissionCreate(part_name="bracket"),
        None,
        None,
    )

    sent: list[dict] = []

    async def fake_email(settings, **kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(notify_service, "send_status_change_email", fake_email)

    # SLICING is not in the submitter-notify set.
    await notify_service.notify_status_changed(
        db_session,
        settings,
        submission=sub,
        from_status=SubmissionStatus.QUEUED,
        to_status=SubmissionStatus.SLICING,
    )
    assert sent == []


@pytest.mark.asyncio
async def test_failure_triggers_webhook(db_session, submitter, settings, monkeypatch):
    from app.storage import get_storage
    from app.submissions import service as sub_service
    from app.submissions.schemas import SubmissionCreate

    sub = await sub_service.create_submission(
        db_session,
        settings,
        get_storage(),
        submitter,
        SubmissionCreate(part_name="bracket"),
        None,
        None,
    )

    webhook_calls: list[dict] = []

    async def fake_webhook(settings, **kwargs):
        webhook_calls.append(kwargs)

    async def fake_email(settings, **kwargs):
        pass

    monkeypatch.setattr(notify_service, "notify_failure", fake_webhook)
    monkeypatch.setattr(notify_service, "send_status_change_email", fake_email)

    await notify_service.notify_status_changed(
        db_session,
        settings,
        submission=sub,
        from_status=SubmissionStatus.PRINTING,
        to_status=SubmissionStatus.FAILED,
    )
    assert len(webhook_calls) == 1
    assert webhook_calls[0]["submission_part_name"] == "bracket"


@pytest.mark.asyncio
async def test_done_does_not_trigger_webhook(db_session, submitter, settings, monkeypatch):
    from app.storage import get_storage
    from app.submissions import service as sub_service
    from app.submissions.schemas import SubmissionCreate

    sub = await sub_service.create_submission(
        db_session,
        settings,
        get_storage(),
        submitter,
        SubmissionCreate(part_name="bracket"),
        None,
        None,
    )

    webhook_calls: list[dict] = []

    async def fake_webhook(settings, **kwargs):
        webhook_calls.append(kwargs)

    async def fake_email(settings, **kwargs):
        pass

    monkeypatch.setattr(notify_service, "notify_failure", fake_webhook)
    monkeypatch.setattr(notify_service, "send_status_change_email", fake_email)

    await notify_service.notify_status_changed(
        db_session,
        settings,
        submission=sub,
        from_status=SubmissionStatus.PRINTING,
        to_status=SubmissionStatus.DONE,
    )
    assert webhook_calls == []


@pytest.mark.asyncio
async def test_webhooks_noop_when_unconfigured(settings, monkeypatch):
    """notify_failure with no webhook URLs configured should not POST anything."""
    from app.notifications import webhooks

    posted: list = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            posted.append((a, k))

    monkeypatch.setattr(webhooks.httpx, "AsyncClient", lambda **k: FakeClient())

    # Settings has empty webhook URLs by default in the test env.
    await webhooks.notify_failure(
        settings,
        submission_part_name="x",
        submitter_email="b@example.com",
        printer_name=None,
        error_code=None,
        site_url="http://testserver",
        submission_url_path="/submissions/1",
    )
    assert posted == []
