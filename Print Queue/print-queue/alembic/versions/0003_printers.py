"""printers and printer_state, plus Submission.target/current_printer_id

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-20

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "printers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(32), nullable=False, unique=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("location", sa.String(120), nullable=True),
        sa.Column("serial", sa.String(64), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_printers_slug", "printers", ["slug"])
    op.create_index("ix_printers_serial", "printers", ["serial"])

    op.create_table(
        "printer_state",
        sa.Column(
            "printer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("printers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "IDLE",
                "PREPARING",
                "PRINTING",
                "PAUSED",
                "FINISHED",
                "FAILED",
                "OFFLINE",
                name="printer_status",
            ),
            nullable=False,
            server_default="OFFLINE",
        ),
        sa.Column("current_file", sa.String(300), nullable=True),
        sa.Column("percent", sa.Float(), nullable=True),
        sa.Column("remaining_minutes", sa.Integer(), nullable=True),
        sa.Column("layer", sa.Integer(), nullable=True),
        sa.Column("total_layers", sa.Integer(), nullable=True),
        sa.Column("nozzle_temp", sa.Float(), nullable=True),
        sa.Column("nozzle_target", sa.Float(), nullable=True),
        sa.Column("bed_temp", sa.Float(), nullable=True),
        sa.Column("bed_target", sa.Float(), nullable=True),
        sa.Column("wifi_signal", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.Integer(), nullable=True),
        sa.Column(
            "current_submission_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("submissions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "raw",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )

    op.add_column(
        "submissions",
        sa.Column(
            "target_printer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("printers.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "submissions",
        sa.Column(
            "current_printer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("printers.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("submissions", "current_printer_id")
    op.drop_column("submissions", "target_printer_id")
    op.drop_table("printer_state")
    op.drop_index("ix_printers_serial", table_name="printers")
    op.drop_index("ix_printers_slug", table_name="printers")
    op.drop_table("printers")
    op.execute("DROP TYPE IF EXISTS printer_status")
