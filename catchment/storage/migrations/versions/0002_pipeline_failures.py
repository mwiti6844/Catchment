"""Pipeline failures: the dead-letter view for the admin review queue.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pipeline_failures",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("error_type", sa.String(128), nullable=False),
        # Exception class or HTTP status only — never a provider message, which
        # can quote the submitted content.
        sa.Column("detail", sa.Text()),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "stage IN ('extraction', 'embedding', 'classification')",
            name="ck_failures_stage",
        ),
    )
    op.create_index(
        "ix_failures_open", "pipeline_failures", ["resolved_at", "occurred_at"]
    )
    op.create_index("ix_failures_item_id", "pipeline_failures", ["item_id"])


def downgrade() -> None:
    op.drop_table("pipeline_failures")
