"""Per-assignment Langfuse trace id, so a tag links back to the call that made it.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable: rule-based assignments (the `unclassified` fallback) have no
    # model call to trace, and existing rows predate tracing entirely.
    op.add_column("item_tags", sa.Column("trace_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("item_tags", "trace_id")
