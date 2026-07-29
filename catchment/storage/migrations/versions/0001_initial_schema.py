"""Initial schema: items, extractions, embeddings, tag graph, review queue.

Revision ID: 0001
Revises:
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = 1024


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(512), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("url", sa.Text()),
        sa.Column("title", sa.Text()),
        sa.Column("author", sa.Text()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("raw_ref", sa.Text()),
        sa.Column("meta", postgresql.JSONB(), nullable=False, server_default="{}"),
        # Ingestion deduplication is a database guarantee, not an application one.
        sa.UniqueConstraint("source", "source_id", name="uq_items_source_source_id"),
        sa.CheckConstraint(
            "source IN ('whatsapp', 'x', 'substack', 'email')", name="ck_items_source"
        ),
        sa.CheckConstraint(
            "kind IN ('text', 'link', 'article', 'image', 'audio', 'video')",
            name="ck_items_kind",
        ),
    )
    op.create_index("ix_items_ingested_at", "items", ["ingested_at"])

    op.create_table(
        "extractions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("extractor", sa.String(64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("language", sa.String(16)),
        sa.Column("confidence", sa.Float()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("meta", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("item_id", "extractor", name="uq_extractions_item_extractor"),
    )
    op.create_index("ix_extractions_item_id", "extractions", ["item_id"])

    op.create_table(
        "embeddings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("vector", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute(
        "CREATE INDEX ix_embeddings_vector_cosine ON embeddings "
        "USING hnsw (vector vector_cosine_ops)"
    )

    op.create_table(
        "tags",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(128), nullable=False, unique=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("origin", sa.String(16), nullable=False, server_default="llm"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column(
            "merged_into_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tags.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("origin IN ('llm', 'human', 'import')", name="ck_tags_origin"),
        sa.CheckConstraint(
            "status IN ('active', 'merged', 'retired')", name="ck_tags_status"
        ),
        sa.CheckConstraint(
            "merged_into_id IS NULL OR merged_into_id <> id", name="ck_tags_no_self_merge"
        ),
    )

    op.create_table(
        "tag_edges",
        sa.Column(
            "parent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "child_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("relation", sa.String(16), nullable=False, server_default="broader"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("parent_id <> child_id", name="ck_tag_edges_no_self_loop"),
    )
    op.create_index("ix_tag_edges_child_id", "tag_edges", ["child_id"])

    op.create_table(
        "item_tags",
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tag_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("assigned_by", sa.String(16), nullable=False, server_default="llm"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0", name="ck_item_tags_confidence"
        ),
        sa.CheckConstraint(
            "assigned_by IN ('llm', 'human', 'import')", name="ck_item_tags_assigned_by"
        ),
    )
    op.create_index("ix_item_tags_tag_id", "item_tags", ["tag_id"])

    op.create_table(
        "taxonomy_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("rationale", sa.Text()),
        sa.Column(
            "proposed_by", sa.String(64), nullable=False, server_default="classifier"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("reviewed_by", sa.String(128)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("kind IN ('merge', 'split')", name="ck_proposals_kind"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'applied')",
            name="ck_proposals_status",
        ),
        # Any non-pending proposal carries the identity of its reviewer.
        sa.CheckConstraint(
            "status = 'pending' OR reviewed_by IS NOT NULL",
            name="ck_proposals_reviewer_recorded",
        ),
        sa.CheckConstraint(
            "applied_at IS NULL OR status = 'applied'", name="ck_proposals_applied_status"
        ),
    )
    op.create_index("ix_proposals_status", "taxonomy_proposals", ["status"])


def downgrade() -> None:
    op.drop_table("taxonomy_proposals")
    op.drop_table("item_tags")
    op.drop_table("tag_edges")
    op.drop_table("tags")
    op.drop_table("embeddings")
    op.drop_table("extractions")
    op.drop_table("items")
