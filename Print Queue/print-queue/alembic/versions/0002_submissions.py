"""submissions, files, submission_events

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-20

"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


# Enum types are declared once with create_type=False on the second use,
# since Postgres only allows a given type name to be created once.
_MATERIAL_VALUES = ("PLA", "PETG", "ABS", "TPU", "ASA")
_PRIORITY_VALUES = ("LOW", "NORMAL", "HIGH", "RUSH")
_STATUS_VALUES = ("QUEUED", "SLICING", "PRINTING", "DONE", "CANCELLED", "FAILED")
_EVENT_TYPE_VALUES = ("CREATED", "EDITED", "STATUS_CHANGED", "FILE_DOWNLOADED", "DELETED")


def upgrade() -> None:
    op.create_table(
        "files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("storage_backend", sa.String(32), nullable=False, server_default="local"),
        sa.Column("storage_key", sa.String(500), nullable=False),
        sa.Column("original_filename", sa.String(300), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "mime_type",
            sa.String(100),
            nullable=False,
            server_default="application/octet-stream",
        ),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_files_sha256", "files", ["sha256"])
    op.create_index("ix_files_expires_at", "files", ["expires_at"])

    op.create_table(
        "submissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "submitter_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("files.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("part_name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "material",
            sa.Enum(*_MATERIAL_VALUES, name="submission_material"),
            nullable=False,
        ),
        sa.Column(
            "priority",
            sa.Enum(*_PRIORITY_VALUES, name="submission_priority"),
            nullable=False,
            server_default="NORMAL",
        ),
        sa.Column(
            "status",
            sa.Enum(*_STATUS_VALUES, name="submission_status"),
            nullable=False,
            server_default="QUEUED",
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_submissions_submitter_id", "submissions", ["submitter_id"])
    op.create_index("ix_submissions_status", "submissions", ["status"])
    op.create_index("ix_submissions_created_at", "submissions", ["created_at"])

    op.create_table(
        "submission_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "submission_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("submissions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "event_type",
            sa.Enum(*_EVENT_TYPE_VALUES, name="submission_event_type"),
            nullable=False,
        ),
        # Reference the enum already created by `submissions`; don't recreate.
        sa.Column(
            "from_status",
            sa.Enum(*_STATUS_VALUES, name="submission_status", create_type=False),
            nullable=True,
        ),
        sa.Column(
            "to_status",
            sa.Enum(*_STATUS_VALUES, name="submission_status", create_type=False),
            nullable=True,
        ),
        sa.Column(
            "by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "event_metadata",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_submission_events_submission_id", "submission_events", ["submission_id"])


def downgrade() -> None:
    op.drop_index("ix_submission_events_submission_id", table_name="submission_events")
    op.drop_table("submission_events")

    op.drop_index("ix_submissions_created_at", table_name="submissions")
    op.drop_index("ix_submissions_status", table_name="submissions")
    op.drop_index("ix_submissions_submitter_id", table_name="submissions")
    op.drop_table("submissions")

    op.drop_index("ix_files_expires_at", table_name="files")
    op.drop_index("ix_files_sha256", table_name="files")
    op.drop_table("files")

    op.execute("DROP TYPE IF EXISTS submission_event_type")
    op.execute("DROP TYPE IF EXISTS submission_status")
    op.execute("DROP TYPE IF EXISTS submission_priority")
    op.execute("DROP TYPE IF EXISTS submission_material")
