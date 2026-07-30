"""Connector liveness, so a silent source is visible before you notice absence.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "connector_health",
        sa.Column("source", sa.String(32), primary_key=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column(
            "last_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_outcome", sa.String(16), nullable=False),
        sa.Column("detail", sa.String(128)),
        sa.Column("items_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_created", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint(
            "source IN ('whatsapp', 'x', 'substack', 'email')", name="ck_health_source"
        ),
        sa.CheckConstraint(
            "last_outcome IN ('success', 'failure')", name="ck_health_outcome"
        ),
    )


def downgrade() -> None:
    op.drop_table("connector_health")
