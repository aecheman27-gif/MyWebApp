"""Form input schemas. We parse multipart form data into these for
validation before touching the database.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.models.submission import SubmissionMaterial, SubmissionPriority, SubmissionStatus


class SubmissionCreate(BaseModel):
    part_name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    material: SubmissionMaterial = SubmissionMaterial.PLA
    priority: SubmissionPriority = SubmissionPriority.NORMAL
    notes: str | None = Field(default=None, max_length=5000)

    @field_validator("description", "notes", mode="before")
    @classmethod
    def empty_string_to_none(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("part_name", mode="before")
    @classmethod
    def strip_part_name(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v


class SubmissionEdit(SubmissionCreate):
    """Same fields as create — we re-validate everything on edit."""


class StatusChange(BaseModel):
    to_status: SubmissionStatus
