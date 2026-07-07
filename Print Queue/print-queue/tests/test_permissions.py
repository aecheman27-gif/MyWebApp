"""Submission permission rules."""

from __future__ import annotations

import pytest

from app.models.submission import Submission, SubmissionStatus
from app.submissions import permissions


@pytest.mark.asyncio
async def test_operator_can_edit_any_submission(db_session, operator, submitter):
    sub = Submission(submitter_id=submitter.id, part_name="x", status=SubmissionStatus.PRINTING)
    db_session.add(sub)
    await db_session.commit()
    assert permissions.can_edit_submission(operator, sub) is True


@pytest.mark.asyncio
async def test_submitter_can_edit_own_queued(db_session, submitter):
    sub = Submission(submitter_id=submitter.id, part_name="x", status=SubmissionStatus.QUEUED)
    db_session.add(sub)
    await db_session.commit()
    assert permissions.can_edit_submission(submitter, sub) is True


@pytest.mark.asyncio
async def test_submitter_cannot_edit_own_in_progress(db_session, submitter):
    sub = Submission(submitter_id=submitter.id, part_name="x", status=SubmissionStatus.PRINTING)
    db_session.add(sub)
    await db_session.commit()
    assert permissions.can_edit_submission(submitter, sub) is False


@pytest.mark.asyncio
async def test_submitter_cannot_edit_others(db_session, submitter, other_submitter):
    sub = Submission(submitter_id=other_submitter.id, part_name="x", status=SubmissionStatus.QUEUED)
    db_session.add(sub)
    await db_session.commit()
    assert permissions.can_edit_submission(submitter, sub) is False


@pytest.mark.asyncio
async def test_only_operator_can_change_status(operator, submitter):
    assert permissions.can_change_status(operator) is True
    assert permissions.can_change_status(submitter) is False


@pytest.mark.asyncio
async def test_submitter_can_download_own(db_session, submitter):
    sub = Submission(submitter_id=submitter.id, part_name="x")
    db_session.add(sub)
    await db_session.commit()
    assert permissions.can_download_file(submitter, sub) is True


@pytest.mark.asyncio
async def test_submitter_cannot_download_others(db_session, submitter, other_submitter):
    sub = Submission(submitter_id=other_submitter.id, part_name="x")
    db_session.add(sub)
    await db_session.commit()
    assert permissions.can_download_file(submitter, sub) is False


@pytest.mark.asyncio
async def test_operator_can_download_anyone(db_session, operator, submitter):
    sub = Submission(submitter_id=submitter.id, part_name="x")
    db_session.add(sub)
    await db_session.commit()
    assert permissions.can_download_file(operator, sub) is True
