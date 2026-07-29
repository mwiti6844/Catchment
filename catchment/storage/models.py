"""SQLAlchemy models. See ``docs/schema.md`` for column-level rationale.

Two schema-level invariants from CLAUDE.md are enforced here rather than in
application code:

* ingestion is keyed by ``(source, source_id)`` under a unique constraint;
* taxonomy merges/splits live in a review queue and carry an explicit status,
  so nothing can be applied without a recorded human decision.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

#: Dimensionality of BGE-M3 embeddings.
EMBEDDING_DIM: Final[int] = 1024

SOURCES: Final[tuple[str, ...]] = ("whatsapp", "x", "substack", "email")
ITEM_KINDS: Final[tuple[str, ...]] = ("text", "link", "article", "image", "audio", "video")
ORIGINS: Final[tuple[str, ...]] = ("llm", "human", "import")
TAG_STATUSES: Final[tuple[str, ...]] = ("active", "merged", "retired")
PROPOSAL_KINDS: Final[tuple[str, ...]] = ("merge", "split")
PROPOSAL_STATUSES: Final[tuple[str, ...]] = ("pending", "approved", "rejected", "applied")
PIPELINE_STAGES: Final[tuple[str, ...]] = ("extraction", "embedding", "classification")


def _in_set(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


class Base(DeclarativeBase):
    """Declarative base for all Catchment tables."""


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Item(Base):
    """One ingested artefact from one source.

    ``(source, source_id)`` is unique at the database level — deduplication is
    a constraint, not a best-effort application check.
    """

    __tablename__ = "items"

    id: Mapped[uuid.UUID] = _pk()
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = _created_at()
    # Pointer to blob storage for media. Bytes never live in this table.
    raw_ref: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    extractions: Mapped[list[Extraction]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )
    embedding: Mapped[Embedding | None] = relationship(
        back_populates="item", cascade="all, delete-orphan", uselist=False
    )
    tag_links: Mapped[list[ItemTag]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_items_source_source_id"),
        CheckConstraint(_in_set("source", SOURCES), name="ck_items_source"),
        CheckConstraint(_in_set("kind", ITEM_KINDS), name="ck_items_kind"),
        Index("ix_items_ingested_at", "ingested_at"),
    )


class Extraction(Base):
    """Text recovered from an item — parsed article, OCR output, or transcript."""

    __tablename__ = "extractions"

    id: Mapped[uuid.UUID] = _pk()
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), nullable=False
    )
    extractor: Mapped[str] = mapped_column(String(64), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(16))
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = _created_at()
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    item: Mapped[Item] = relationship(back_populates="extractions")

    __table_args__ = (
        UniqueConstraint("item_id", "extractor", name="uq_extractions_item_extractor"),
        Index("ix_extractions_item_id", "item_id"),
    )


class Embedding(Base):
    """BGE-M3 vector for an item, stored in pgvector."""

    __tablename__ = "embeddings"

    id: Mapped[uuid.UUID] = _pk()
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False, default=EMBEDDING_DIM)
    vector: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    created_at: Mapped[datetime] = _created_at()

    item: Mapped[Item] = relationship(back_populates="embedding")

    __table_args__ = (
        # Mirrors the raw ``CREATE INDEX`` in migration 0001. Declared here so
        # ``Base.metadata.create_all`` (used by the test fixtures) builds the
        # same index production has — without this the two drift and nothing
        # errors. The operator class must stay ``vector_cosine_ops``: it is what
        # makes ``ItemRepository.nearest``'s ``<=>`` ordering index-backed.
        Index(
            "ix_embeddings_vector_cosine",
            "vector",
            postgresql_using="hnsw",
            postgresql_ops={"vector": "vector_cosine_ops"},
        ),
    )


class Tag(Base):
    """A node in the dynamic tag graph. Tags are created by the classifier, not
    drawn from a fixed taxonomy."""

    __tablename__ = "tags"

    id: Mapped[uuid.UUID] = _pk()
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="llm")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tags.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint(_in_set("origin", ORIGINS), name="ck_tags_origin"),
        CheckConstraint(_in_set("status", TAG_STATUSES), name="ck_tags_status"),
        CheckConstraint(
            "merged_into_id IS NULL OR merged_into_id <> id",
            name="ck_tags_no_self_merge",
        ),
    )


class TagEdge(Base):
    """A directed edge in the tag graph (``parent`` is broader than ``child``)."""

    __tablename__ = "tag_edges"

    parent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )
    child_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )
    relation: Mapped[str] = mapped_column(String(16), nullable=False, default="broader")
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint("parent_id <> child_id", name="ck_tag_edges_no_self_loop"),
        Index("ix_tag_edges_child_id", "child_id"),
    )


class ItemTag(Base):
    """Assignment of a tag to an item, with the classifier's confidence."""

    __tablename__ = "item_tags"

    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    assigned_by: Mapped[str] = mapped_column(String(16), nullable=False, default="llm")
    created_at: Mapped[datetime] = _created_at()

    item: Mapped[Item] = relationship(back_populates="tag_links")
    tag: Mapped[Tag] = relationship()

    __table_args__ = (
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0", name="ck_item_tags_confidence"
        ),
        CheckConstraint(_in_set("assigned_by", ORIGINS), name="ck_item_tags_assigned_by"),
        Index("ix_item_tags_tag_id", "tag_id"),
    )


class PipelineFailure(Base):
    """A stage that degraded, recorded where a human can see it.

    Classification failures fall back to the ``unclassified`` tag rather than
    failing the job, which keeps ingestion resilient but makes the failure
    invisible: nothing in Postgres distinguishes "no text to classify" from
    "the classifier errored". RQ's own failed-job registry lives in Redis,
    which Appsmith cannot read. This table is the dead-letter view.

    ``detail`` holds an exception class name or an HTTP status — never a
    provider message, which can quote the submitted content.
    """

    __tablename__ = "pipeline_failures"

    id: Mapped[uuid.UUID] = _pk()
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    error_type: Mapped[str] = mapped_column(String(128), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = _created_at()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    item: Mapped[Item] = relationship()

    __table_args__ = (
        CheckConstraint(_in_set("stage", PIPELINE_STAGES), name="ck_failures_stage"),
        Index("ix_failures_open", "resolved_at", "occurred_at"),
        Index("ix_failures_item_id", "item_id"),
    )


class TaxonomyProposal(Base):
    """A proposed merge or split of tags, awaiting human review.

    Merges and splits are never auto-executed: a row lands here as ``pending``
    and only a recorded approval moves it forward.
    """

    __tablename__ = "taxonomy_proposals"

    id: Mapped[uuid.UUID] = _pk()
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    proposed_by: Mapped[str] = mapped_column(String(64), nullable=False, default="classifier")
    created_at: Mapped[datetime] = _created_at()
    reviewed_by: Mapped[str | None] = mapped_column(String(128))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(_in_set("kind", PROPOSAL_KINDS), name="ck_proposals_kind"),
        CheckConstraint(_in_set("status", PROPOSAL_STATUSES), name="ck_proposals_status"),
        # A decision must carry the identity of whoever made it.
        CheckConstraint(
            "status = 'pending' OR reviewed_by IS NOT NULL",
            name="ck_proposals_reviewer_recorded",
        ),
        # Nothing is applied without having been approved first.
        CheckConstraint(
            "applied_at IS NULL OR status = 'applied'",
            name="ck_proposals_applied_status",
        ),
        Index("ix_proposals_status", "status"),
    )
