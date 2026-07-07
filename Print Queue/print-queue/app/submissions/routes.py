"""Submission HTTP routes.

GET  /                              -> queue dashboard (default /)
GET  /submissions/new               -> creation form
POST /submissions                   -> create submission
GET  /submissions/{id}              -> detail
POST /submissions/{id}/edit         -> edit (own if QUEUED; operator any)
POST /submissions/{id}/delete       -> delete (same rule)
POST /submissions/{id}/status       -> operator status transition (HTMX swap)
GET  /submissions/{id}/download     -> stream the STEP file (auth scoped)
"""

from __future__ import annotations

from uuid import UUID

import structlog
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user
from app.config import Settings, get_settings
from app.database import get_db
from app.models.submission import (
    SubmissionMaterial,
    SubmissionPriority,
    SubmissionStatus,
)
from app.models.user import User
from app.printers.service import list_printer_states
from app.storage import FileStorage, get_storage
from app.submissions import permissions, service
from app.submissions.schemas import StatusChange, SubmissionCreate, SubmissionEdit

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
log = structlog.get_logger(__name__)


def _storage_dep() -> FileStorage:
    return get_storage()


@router.get("/", response_class=HTMLResponse)
async def queue_view(
    request: Request,
    filter: str = Query(default="active"),
    search: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if filter not in ("mine", "active", "in_progress", "completed", "all"):
        filter = "active"
    submissions = await service.list_submissions(
        db,
        filter_=filter,  # type: ignore[arg-type]
        user=user,
        search=search,
    )
    printer_states = await list_printer_states(db)
    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "site_name": settings.site_name,
            "user": user,
            "submissions": submissions,
            "printer_states": printer_states,
            "active_filter": filter,
            "search": search or "",
            "is_operator": permissions.is_operator(user),
        },
    )


@router.get("/submissions/new", response_class=HTMLResponse)
async def submission_form(
    request: Request,
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    return templates.TemplateResponse(
        request,
        "submission_new.html",
        {
            "site_name": settings.site_name,
            "user": user,
            "materials": list(SubmissionMaterial),
            "priorities": list(SubmissionPriority),
            "max_upload_mb": settings.max_upload_mb,
        },
    )


@router.post("/submissions")
async def create_submission_endpoint(
    request: Request,
    part_name: str = Form(...),
    description: str | None = Form(default=None),
    material: str = Form(default="PLA"),
    priority: str = Form(default="NORMAL"),
    notes: str | None = Form(default=None),
    file: UploadFile | None = File(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    storage: FileStorage = Depends(_storage_dep),
):
    try:
        data = SubmissionCreate(
            part_name=part_name,
            description=description,
            material=material,  # type: ignore[arg-type]
            priority=priority,  # type: ignore[arg-type]
            notes=notes,
        )
    except ValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    file_bytes: bytes | None = None
    file_name: str | None = None
    file_mime = "application/octet-stream"
    if file is not None and file.filename:
        file_bytes = await file.read()
        file_name = file.filename
        if file.content_type:
            file_mime = file.content_type

    submission = await service.create_submission(
        db,
        settings,
        storage,
        user,
        data,
        file_bytes,
        file_name,
        file_mime,
    )
    return RedirectResponse(
        url=f"/submissions/{submission.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/submissions/{submission_id}", response_class=HTMLResponse)
async def submission_detail(
    request: Request,
    submission_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    submission = await service.get_submission(db, submission_id)
    if submission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "submission not found")

    from app.comments import service as comment_service

    comments = await comment_service.list_for_submission(db, submission.id)

    return templates.TemplateResponse(
        request,
        "submission_detail.html",
        {
            "site_name": settings.site_name,
            "user": user,
            "submission": submission,
            "comments": comments,
            "can_edit": permissions.can_edit_submission(user, submission),
            "can_delete": permissions.can_delete_submission(user, submission),
            "can_change_status": permissions.can_change_status(user),
            "can_download": permissions.can_download_file(user, submission),
            "all_statuses": list(SubmissionStatus),
            "materials": list(SubmissionMaterial),
            "priorities": list(SubmissionPriority),
        },
    )


@router.post("/submissions/{submission_id}/edit")
async def edit_submission_endpoint(
    submission_id: UUID,
    part_name: str = Form(...),
    description: str | None = Form(default=None),
    material: str = Form(default="PLA"),
    priority: str = Form(default="NORMAL"),
    notes: str | None = Form(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    submission = await service.get_submission(db, submission_id)
    if submission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "submission not found")
    if not permissions.can_edit_submission(user, submission):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not allowed to edit")

    try:
        data = SubmissionEdit(
            part_name=part_name,
            description=description,
            material=material,  # type: ignore[arg-type]
            priority=priority,  # type: ignore[arg-type]
            notes=notes,
        )
    except ValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    await service.edit_submission(db, submission, user, data)
    return RedirectResponse(
        url=f"/submissions/{submission_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/submissions/{submission_id}/delete")
async def delete_submission_endpoint(
    submission_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: FileStorage = Depends(_storage_dep),
):
    submission = await service.get_submission(db, submission_id)
    if submission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "submission not found")
    if not permissions.can_delete_submission(user, submission):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not allowed to delete")
    await service.delete_submission(db, storage, submission, user)
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/submissions/{submission_id}/status", response_class=HTMLResponse)
async def change_status_endpoint(
    request: Request,
    submission_id: UUID,
    to_status: str = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not permissions.can_change_status(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "operator role required")
    submission = await service.get_submission(db, submission_id)
    if submission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "submission not found")

    try:
        change = StatusChange(to_status=to_status)  # type: ignore[arg-type]
    except ValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    await service.change_status(db, submission, user, change.to_status, settings=settings)

    # If the request is an HTMX swap, return just the row partial.
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request,
            "_submission_row.html",
            {
                "user": user,
                "submission": submission,
                "is_operator": permissions.is_operator(user),
                "all_statuses": list(SubmissionStatus),
            },
        )
    # Otherwise redirect back to the detail page.
    return RedirectResponse(
        url=f"/submissions/{submission_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/submissions/{submission_id}/download")
async def download_file(
    submission_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: FileStorage = Depends(_storage_dep),
):
    submission = await service.get_submission(db, submission_id)
    if submission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "submission not found")
    if not permissions.can_download_file(user, submission):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not allowed to download")
    if submission.file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no file attached")

    f = submission.file
    return StreamingResponse(
        storage.stream(f.storage_key),
        media_type=f.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{f.original_filename}"',
            "Content-Length": str(f.size_bytes),
        },
    )


@router.post("/submissions/{submission_id}/comments")
async def add_comment_endpoint(
    submission_id: UUID,
    body: str = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Add a comment. Anyone who can view the submission can comment.

    Permission model: same as detail view — submitter sees own; operator
    sees any. We re-fetch and check via service.get_submission which
    enforces scoping. Operators may also need to comment on others' work,
    which the existing detail-view check already allows.
    """
    submission = await service.get_submission(db, submission_id)
    if submission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "submission not found")
    if not permissions.can_view(user, submission):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not allowed")

    from app.comments import service as comment_service

    try:
        await comment_service.create(
            db,
            settings,
            submission=submission,
            author=user,
            body=body,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    return RedirectResponse(
        f"/submissions/{submission.id}#comments",
        status_code=status.HTTP_303_SEE_OTHER,
    )
