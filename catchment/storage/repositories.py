"""Repository layer. Every database write in the codebase goes through here —
connectors and services must not issue queries of their own (CLAUDE.md).

Two rules are load-bearing and enforced in this module:

* recursive walks over the tag graph always carry an explicit depth bound;
* taxonomy merges and splits are proposed into a review queue and can only be
  applied after a recorded human approval.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import Select, and_, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from catchment.config import TAG_DEPTH_HARD_CEILING
from catchment.logging_config import get_logger, log_context
from catchment.storage.models import (
    Embedding,
    Extraction,
    Item,
    ItemTag,
    PipelineFailure,
    Tag,
    TagEdge,
    TaxonomyProposal,
)

logger = get_logger(__name__)

DEFAULT_TAG_DEPTH: Final[int] = 8


class RepositoryError(RuntimeError):
    """Base class for repository-level failures."""


class UnboundedTraversalError(RepositoryError):
    """Raised when a graph walk is requested without a usable depth bound."""


class ProposalNotApprovedError(RepositoryError):
    """Raised when a taxonomy change is applied without a recorded approval."""


@dataclass(frozen=True, slots=True)
class TagRef:
    """A tag reached during a graph walk, with its distance from the origin."""

    tag_id: uuid.UUID
    depth: int


def _validate_depth(max_depth: int) -> int:
    if max_depth < 1 or max_depth > TAG_DEPTH_HARD_CEILING:
        raise UnboundedTraversalError(
            f"max_depth must be between 1 and {TAG_DEPTH_HARD_CEILING}, got {max_depth}"
        )
    return max_depth


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Items
# --------------------------------------------------------------------------- #


class ItemRepository:
    """Reads and writes for ingested items and their derived artefacts."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(
        self,
        *,
        source: str,
        source_id: str,
        kind: str,
        url: str | None = None,
        title: str | None = None,
        author: str | None = None,
        published_at: datetime | None = None,
        raw_ref: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> tuple[Item, bool]:
        """Insert an item, or return the existing one for ``(source, source_id)``.

        Returns ``(item, created)``. Deduplication rests on the unique
        constraint, so concurrent ingestion of the same message is safe.
        """
        stmt = (
            pg_insert(Item)
            .values(
                source=source,
                source_id=source_id,
                kind=kind,
                url=url,
                title=title,
                author=author,
                published_at=published_at,
                raw_ref=raw_ref,
                meta=meta or {},
            )
            .on_conflict_do_nothing(constraint="uq_items_source_source_id")
            .returning(Item.id)
        )
        inserted_id = self._session.execute(stmt).scalar_one_or_none()

        if inserted_id is None:
            existing = self.get_by_source(source=source, source_id=source_id)
            if existing is None:  # pragma: no cover — only on a concurrent delete
                raise RepositoryError(
                    f"item {source}:{source_id} vanished between insert and read"
                )
            return existing, False

        item = self._session.get_one(Item, inserted_id)
        logger.info(
            "item ingested",
            extra=log_context(item_id=str(item.id), source=source, kind=kind),
        )
        return item, True

    def get_by_source(self, *, source: str, source_id: str) -> Item | None:
        stmt = select(Item).where(
            and_(Item.source == source, Item.source_id == source_id)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def get(self, item_id: uuid.UUID) -> Item | None:
        return self._session.get(Item, item_id)

    def add_extraction(
        self,
        *,
        item_id: uuid.UUID,
        extractor: str,
        text: str,
        language: str | None = None,
        confidence: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Extraction:
        """Record extracted text. Re-running an extractor replaces its output."""
        stmt = (
            pg_insert(Extraction)
            .values(
                item_id=item_id,
                extractor=extractor,
                text=text,
                language=language,
                confidence=confidence,
                meta=meta or {},
            )
            .on_conflict_do_update(
                constraint="uq_extractions_item_extractor",
                set_={"text": text, "language": language, "confidence": confidence},
            )
            .returning(Extraction.id)
        )
        extraction_id = self._session.execute(stmt).scalar_one()
        # Length only — never the extracted text itself.
        logger.info(
            "extraction stored",
            extra=log_context(
                item_id=str(item_id),
                extractor=extractor,
                chars=len(text),
            ),
        )
        return self._session.get_one(Extraction, extraction_id)

    def set_embedding(
        self, *, item_id: uuid.UUID, model: str, vector: list[float]
    ) -> Embedding:
        """Store or replace an item's embedding."""
        stmt = (
            pg_insert(Embedding)
            .values(item_id=item_id, model=model, dim=len(vector), vector=vector)
            .on_conflict_do_update(
                index_elements=[Embedding.item_id],
                set_={"model": model, "dim": len(vector), "vector": vector},
            )
            .returning(Embedding.id)
        )
        embedding_id = self._session.execute(stmt).scalar_one()
        return self._session.get_one(Embedding, embedding_id)

    def nearest(
        self, *, vector: list[float], limit: int = 10, exclude: uuid.UUID | None = None
    ) -> list[tuple[Item, float]]:
        """Return the ``limit`` nearest items by cosine distance."""
        distance = Embedding.vector.cosine_distance(vector).label("distance")
        stmt = (
            select(Item, distance)
            .join(Embedding, Embedding.item_id == Item.id)
            .order_by(distance)
            .limit(limit)
        )
        if exclude is not None:
            stmt = stmt.where(Item.id != exclude)
        return [(row[0], row[1]) for row in self._session.execute(stmt).all()]


# --------------------------------------------------------------------------- #
# Tag graph
# --------------------------------------------------------------------------- #


def build_ancestors_stmt(tag_id: uuid.UUID, max_depth: int) -> Select[tuple[uuid.UUID, int]]:
    """Build a depth-bounded recursive CTE walking upward from ``tag_id``.

    Exposed separately from :class:`TagRepository` so the depth bound can be
    asserted in tests without a live database.
    """
    depth = _validate_depth(max_depth)

    base = select(
        TagEdge.parent_id.label("tag_id"), literal(1).label("depth")
    ).where(TagEdge.child_id == tag_id)
    cte = base.cte("tag_ancestors", recursive=True)
    step = (
        select(TagEdge.parent_id, (cte.c.depth + 1).label("depth"))
        .join(cte, TagEdge.child_id == cte.c.tag_id)
        .where(cte.c.depth < depth)
    )
    walk = cte.union_all(step)
    return select(walk.c.tag_id, walk.c.depth)


def build_descendants_stmt(
    tag_id: uuid.UUID, max_depth: int
) -> Select[tuple[uuid.UUID, int]]:
    """Build a depth-bounded recursive CTE walking downward from ``tag_id``."""
    depth = _validate_depth(max_depth)

    base = select(
        TagEdge.child_id.label("tag_id"), literal(1).label("depth")
    ).where(TagEdge.parent_id == tag_id)
    cte = base.cte("tag_descendants", recursive=True)
    step = (
        select(TagEdge.child_id, (cte.c.depth + 1).label("depth"))
        .join(cte, TagEdge.parent_id == cte.c.tag_id)
        .where(cte.c.depth < depth)
    )
    walk = cte.union_all(step)
    return select(walk.c.tag_id, walk.c.depth)


class TagRepository:
    """Reads and writes for the dynamic tag graph."""

    def __init__(self, session: Session, *, max_depth: int = DEFAULT_TAG_DEPTH) -> None:
        self._session = session
        self._max_depth = _validate_depth(max_depth)

    def get_or_create(
        self,
        *,
        slug: str,
        label: str,
        description: str | None = None,
        origin: str = "llm",
    ) -> tuple[Tag, bool]:
        """Fetch a tag by slug, creating it if the classifier has coined a new one."""
        stmt = (
            pg_insert(Tag)
            .values(slug=slug, label=label, description=description, origin=origin)
            .on_conflict_do_nothing(index_elements=[Tag.slug])
            .returning(Tag.id)
        )
        created_id = self._session.execute(stmt).scalar_one_or_none()
        if created_id is None:
            existing = self._session.execute(
                select(Tag).where(Tag.slug == slug)
            ).scalar_one()
            return existing, False

        logger.info("tag created", extra=log_context(tag_id=str(created_id), slug=slug))
        return self._session.get_one(Tag, created_id), True

    def add_edge(
        self, *, parent_id: uuid.UUID, child_id: uuid.UUID, relation: str = "broader"
    ) -> None:
        """Link two tags. Self-loops are rejected by a check constraint."""
        stmt = (
            pg_insert(TagEdge)
            .values(parent_id=parent_id, child_id=child_id, relation=relation)
            .on_conflict_do_nothing(index_elements=[TagEdge.parent_id, TagEdge.child_id])
        )
        self._session.execute(stmt)

    def ancestors(self, tag_id: uuid.UUID, *, max_depth: int | None = None) -> list[TagRef]:
        """Return ancestors of ``tag_id``, never walking deeper than the bound."""
        stmt = build_ancestors_stmt(tag_id, max_depth or self._max_depth)
        return [TagRef(tag_id=row[0], depth=row[1]) for row in self._session.execute(stmt)]

    def descendants(
        self, tag_id: uuid.UUID, *, max_depth: int | None = None
    ) -> list[TagRef]:
        """Return descendants of ``tag_id``, never walking deeper than the bound."""
        stmt = build_descendants_stmt(tag_id, max_depth or self._max_depth)
        return [TagRef(tag_id=row[0], depth=row[1]) for row in self._session.execute(stmt)]

    def labels_for_items(self, item_ids: Sequence[uuid.UUID]) -> list[str]:
        """Return the distinct active tag labels applied to ``item_ids``.

        This is the candidate list handed to the classifier. Merged and retired
        tags are excluded: offering the classifier a tag that has been merged
        away would reintroduce the duplicate a human just resolved.
        """
        if not item_ids:
            return []

        stmt = (
            select(Tag.label)
            .join(ItemTag, ItemTag.tag_id == Tag.id)
            .where(and_(ItemTag.item_id.in_(item_ids), Tag.status == "active"))
            .distinct()
            .order_by(Tag.label)
        )
        return list(self._session.execute(stmt).scalars())

    def existing_slugs(self, slugs: Sequence[str]) -> set[str]:
        """Return which of ``slugs`` already exist, regardless of status.

        The classifier's candidate list only carries tags from *nearby* items,
        so a suggestion absent from it is not necessarily new to the graph.
        Deciding novelty from the candidate list alone would block legitimate
        reuse of a tag that exists but happens to sit far away.
        """
        if not slugs:
            return set()
        stmt = select(Tag.slug).where(Tag.slug.in_(list(slugs)))
        return set(self._session.execute(stmt).scalars())

    def assign(
        self,
        *,
        item_id: uuid.UUID,
        tag_id: uuid.UUID,
        confidence: float,
        assigned_by: str = "llm",
        trace_id: str | None = None,
    ) -> None:
        """Attach a tag to an item, updating confidence if already attached."""
        stmt = (
            pg_insert(ItemTag)
            .values(
                item_id=item_id,
                tag_id=tag_id,
                confidence=confidence,
                assigned_by=assigned_by,
                trace_id=trace_id,
            )
            .on_conflict_do_update(
                index_elements=[ItemTag.item_id, ItemTag.tag_id],
                set_={
                    "confidence": confidence,
                    "assigned_by": assigned_by,
                    "trace_id": trace_id,
                },
            )
        )
        self._session.execute(stmt)


# --------------------------------------------------------------------------- #
# Dead-letter view
# --------------------------------------------------------------------------- #


class PipelineFailureRepository:
    """Records degraded pipeline stages so a human can see them.

    The pipeline deliberately degrades rather than failing — an item with a
    classifier outage still lands in the review queue. Without this table that
    resilience is indistinguishable from success, because nothing in Postgres
    says why an item is ``unclassified``.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        *,
        item_id: uuid.UUID,
        stage: str,
        error_type: str,
        detail: str | None = None,
    ) -> PipelineFailure:
        """Record one failure.

        ``detail`` must be an exception class, status code, or similar. Never
        pass a provider message: several quote the submitted content back.
        """
        failure = PipelineFailure(
            item_id=item_id, stage=stage, error_type=error_type, detail=detail
        )
        self._session.add(failure)
        self._session.flush()
        logger.warning(
            "pipeline stage failed",
            extra=log_context(
                item_id=str(item_id), stage=stage, error_type=error_type
            ),
        )
        return failure

    def list_open(self, *, limit: int = 100) -> list[PipelineFailure]:
        """Return unresolved failures, oldest first."""
        stmt = (
            select(PipelineFailure)
            .where(PipelineFailure.resolved_at.is_(None))
            .order_by(PipelineFailure.occurred_at)
            .limit(limit)
        )
        return list(self._session.execute(stmt).scalars())

    def resolve(self, failure_id: uuid.UUID) -> PipelineFailure:
        """Mark a failure handled, e.g. after a successful re-run."""
        failure = self._session.get(PipelineFailure, failure_id)
        if failure is None:
            raise RepositoryError(f"failure {failure_id} does not exist")
        failure.resolved_at = _now()
        self._session.flush()
        return failure


# --------------------------------------------------------------------------- #
# Taxonomy review queue
# --------------------------------------------------------------------------- #


class TaxonomyProposalRepository:
    """The review queue. Merges and splits are proposed here, never executed
    directly by the classifier."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def propose_merge(
        self,
        *,
        source_tag_ids: list[uuid.UUID],
        target_tag_id: uuid.UUID,
        rationale: str | None = None,
        proposed_by: str = "classifier",
    ) -> TaxonomyProposal:
        """Queue a merge for human review. Nothing is changed in the graph."""
        return self._create(
            kind="merge",
            payload={
                "source_tag_ids": [str(tag_id) for tag_id in source_tag_ids],
                "target_tag_id": str(target_tag_id),
            },
            rationale=rationale,
            proposed_by=proposed_by,
        )

    def propose_split(
        self,
        *,
        tag_id: uuid.UUID,
        into: list[dict[str, str]],
        rationale: str | None = None,
        proposed_by: str = "classifier",
    ) -> TaxonomyProposal:
        """Queue a split for human review. Nothing is changed in the graph."""
        return self._create(
            kind="split",
            payload={"tag_id": str(tag_id), "into": into},
            rationale=rationale,
            proposed_by=proposed_by,
        )

    def _create(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        rationale: str | None,
        proposed_by: str,
    ) -> TaxonomyProposal:
        proposal = TaxonomyProposal(
            kind=kind,
            status="pending",
            payload=payload,
            rationale=rationale,
            proposed_by=proposed_by,
        )
        self._session.add(proposal)
        self._session.flush()
        logger.info(
            "taxonomy change proposed",
            extra=log_context(proposal_id=str(proposal.id), kind=kind, status="pending"),
        )
        return proposal

    def list_pending(self, *, limit: int = 100) -> list[TaxonomyProposal]:
        stmt = (
            select(TaxonomyProposal)
            .where(TaxonomyProposal.status == "pending")
            .order_by(TaxonomyProposal.created_at)
            .limit(limit)
        )
        return list(self._session.execute(stmt).scalars())

    def approve(self, proposal_id: uuid.UUID, *, reviewer: str) -> TaxonomyProposal:
        """Record a human approval. Approval alone does not apply the change."""
        return self._decide(proposal_id, status="approved", reviewer=reviewer)

    def reject(self, proposal_id: uuid.UUID, *, reviewer: str) -> TaxonomyProposal:
        return self._decide(proposal_id, status="rejected", reviewer=reviewer)

    def _decide(
        self, proposal_id: uuid.UUID, *, status: str, reviewer: str
    ) -> TaxonomyProposal:
        if not reviewer.strip():
            raise RepositoryError("a reviewer identity is required to decide a proposal")
        stmt = (
            update(TaxonomyProposal)
            .where(
                and_(
                    TaxonomyProposal.id == proposal_id,
                    TaxonomyProposal.status == "pending",
                )
            )
            .values(status=status, reviewed_by=reviewer, reviewed_at=_now())
            .returning(TaxonomyProposal.id)
        )
        if self._session.execute(stmt).scalar_one_or_none() is None:
            raise RepositoryError(f"proposal {proposal_id} is not pending review")
        logger.info(
            "taxonomy proposal decided",
            extra=log_context(proposal_id=str(proposal_id), status=status),
        )
        return self._session.get_one(TaxonomyProposal, proposal_id)

    def mark_applied(self, proposal_id: uuid.UUID) -> TaxonomyProposal:
        """Flip an approved proposal to ``applied``.

        Callers must have executed the graph change already. A proposal that
        was never approved cannot reach this state.
        """
        proposal = self._session.get(TaxonomyProposal, proposal_id)
        if proposal is None:
            raise RepositoryError(f"proposal {proposal_id} does not exist")
        if proposal.status != "approved":
            raise ProposalNotApprovedError(
                f"proposal {proposal_id} is {proposal.status!r}; only approved "
                "proposals may be applied"
            )
        proposal.status = "applied"
        proposal.applied_at = _now()
        self._session.flush()
        return proposal
