"""Internal routes for an administrative interface.

Two operations require a trusted backend boundary:

* **Decide a taxonomy proposal.** The decision is a compare-and-swap in
  ``TaxonomyProposalRepository._decide`` — ``UPDATE ... WHERE status='pending'
  RETURNING``. Reimplementing that as a raw query in the dashboard would drop
  the atomicity, so two reviewers racing could both believe they won. This
  wraps the repository and does nothing else.
* **Read the RQ queue.** The queue lives in Redis rather than Postgres and is
  exposed here as a small, stable read model for an admin client.

**These routes authenticate.** ``api`` is reverse-proxied to the internet in
production, so an unauthenticated approval endpoint would put the taxonomy
review gate — "a human approves before the merge runs" — behind nothing at all.
Callers present ``X-Internal-Token``; Caddy additionally refuses
``/internal/*`` from outside. A future admin client must reach the API through
a private network path so it never traverses the public proxy.

No route here returns ingested content.
"""

from __future__ import annotations

import hmac
import uuid
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_

from catchment.classification.embeddings import EmbeddingError, get_embedder
from catchment.config import MissingConfiguration, Settings, get_settings
from catchment.logging_config import get_logger, log_context
from catchment.storage.db import session_scope
from catchment.storage.models import (
    Embedding,
    Extraction,
    Item,
    ItemTag,
    PipelineFailure,
    Tag,
)
from catchment.storage.repositories import (
    ConnectorHealthRepository,
    ItemRepository,
    PipelineFailureRepository,
    RepositoryError,
    TagRepository,
    TaxonomyProposalRepository,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


def require_internal_token(
    x_internal_token: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    """Reject anything without the shared secret.

    Fails closed: if no token is configured the routes are unavailable rather
    than open, so a half-configured deployment cannot expose the review gate.
    """
    try:
        expected = settings.require_internal_token()
    except MissingConfiguration:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="internal routes are not configured",
        ) from None

    if x_internal_token is None or not hmac.compare_digest(
        x_internal_token, expected.get_secret_value()
    ):
        logger.warning("internal route rejected: bad or missing token")
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="forbidden")


class ProposalDecision(BaseModel):
    """A human's decision on a taxonomy proposal."""

    decision: Literal["approve", "reject"]
    #: Recorded on the row. A non-pending proposal without one violates
    #: ck_proposals_reviewer_recorded, so decisions can never be anonymous.
    reviewer: str = Field(min_length=1)

    @field_validator("reviewer")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        """Reject whitespace-only reviewers at the boundary.

        The repository rejects them too, but only after the request is
        accepted — so without this the route advertises `min_length=1` while
        actually requiring non-blank, and a dashboard bug surfaces as a 409
        instead of a validation error naming the field.
        """
        if not value.strip():
            raise ValueError("reviewer must not be blank")
        return value.strip()


class ProposalDecisionResponse(BaseModel):
    id: uuid.UUID
    status: str
    reviewed_by: str | None
    reviewed_at: str | None


@router.post(
    "/proposals/{proposal_id}/decision",
    response_model=ProposalDecisionResponse,
    dependencies=[Depends(require_internal_token)],
)
def decide_proposal(
    proposal_id: uuid.UUID, body: ProposalDecision
) -> ProposalDecisionResponse:
    """Approve or reject a proposal via the repository's compare-and-swap.

    Deciding an already-decided proposal is a 409, not a silent overwrite —
    that is the repository's guarantee surfacing, not a check added here.
    """
    with session_scope() as session:
        proposals = TaxonomyProposalRepository(session)
        try:
            if body.decision == "approve":
                proposal = proposals.approve(proposal_id, reviewer=body.reviewer)
            else:
                proposal = proposals.reject(proposal_id, reviewer=body.reviewer)
        except RepositoryError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from None

        logger.info(
            "proposal decided via dashboard",
            extra=log_context(
                proposal_id=str(proposal_id),
                decision=body.decision,
                reviewer=body.reviewer,
            ),
        )
        return ProposalDecisionResponse(
            id=proposal.id,
            status=proposal.status,
            reviewed_by=proposal.reviewed_by,
            reviewed_at=(
                proposal.reviewed_at.isoformat() if proposal.reviewed_at else None
            ),
        )


class QueueCounts(BaseModel):
    """RQ depth. Counts only — job arguments carry message text."""

    queue: str
    pending: int
    started: int
    finished: int
    failed: int
    deferred: int
    scheduled: int
    #: Age in seconds of the oldest queued job, or None when the queue is empty.
    oldest_pending_seconds: float | None = None


@router.get(
    "/queue", response_model=QueueCounts, dependencies=[Depends(require_internal_token)]
)
def queue_counts() -> QueueCounts:
    """Report RQ depth for the dashboard's Queue page."""
    from catchment.jobs.queue import build_queue

    try:
        return read_queue_counts(build_queue())
    except Exception as error:  # noqa: BLE001 - Redis down must not 500 the page
        logger.warning(
            "queue counts unavailable", extra=log_context(error=type(error).__name__)
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="queue unavailable"
        ) from None


def read_queue_counts(queue: Any) -> QueueCounts:
    """Pull counts off an RQ queue. Split out so it is testable with a fake."""
    oldest: float | None = None
    jobs = queue.get_jobs(0, 0) or []
    if jobs:
        enqueued_at = getattr(jobs[0], "enqueued_at", None)
        if enqueued_at is not None:
            from datetime import UTC, datetime

            reference = enqueued_at
            if reference.tzinfo is None:
                reference = reference.replace(tzinfo=UTC)
            oldest = (datetime.now(UTC) - reference).total_seconds()

    return QueueCounts(
        queue=queue.name,
        pending=queue.count,
        started=queue.started_job_registry.count,
        finished=queue.finished_job_registry.count,
        failed=queue.failed_job_registry.count,
        deferred=queue.deferred_job_registry.count,
        scheduled=queue.scheduled_job_registry.count,
        oldest_pending_seconds=oldest,
    )


# --------------------------------------------------------------------------- #
# Inbox and item detail
# --------------------------------------------------------------------------- #


class ItemSummary(BaseModel):
    """Inbox row. Metadata and a length — not the text itself."""

    id: uuid.UUID
    source: str
    kind: str
    author: str | None
    ingested_at: str
    extracted_chars: int | None
    has_embedding: bool
    status: str
    tag_count: int


class ItemPage(BaseModel):
    items: list[ItemSummary]
    total: int
    limit: int
    offset: int


def _classification_status(
    *, tag_count: int, llm_tags: int, has_extraction: bool, open_failures: int
) -> str:
    """Three states the placeholder tag alone cannot distinguish."""
    if llm_tags:
        return "classified"
    if open_failures:
        return "failed"
    if not has_extraction:
        return "nothing to classify"
    return "pending"


@router.get(
    "/items", response_model=ItemPage, dependencies=[Depends(require_internal_token)]
)
def list_items(
    source: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ItemPage:
    """Paginated inbox, newest first."""
    from sqlalchemy import func, select

    with session_scope() as session:
        chars = select(func.length(Extraction.text)).where(
            Extraction.item_id == Item.id
        ).limit(1).scalar_subquery()
        tag_count = select(func.count()).select_from(ItemTag).where(
            ItemTag.item_id == Item.id
        ).scalar_subquery()
        llm_tags = select(func.count()).select_from(ItemTag).where(
            and_(ItemTag.item_id == Item.id, ItemTag.assigned_by == "llm")
        ).scalar_subquery()
        failures = select(func.count()).select_from(PipelineFailure).where(
            and_(
                PipelineFailure.item_id == Item.id,
                PipelineFailure.resolved_at.is_(None),
            )
        ).scalar_subquery()
        has_embedding = select(func.count()).select_from(Embedding).where(
            Embedding.item_id == Item.id
        ).scalar_subquery()

        stmt = select(
            Item, chars, tag_count, llm_tags, failures, has_embedding
        ).order_by(Item.ingested_at.desc())
        if source:
            stmt = stmt.where(Item.source == source)

        rows = session.execute(stmt).all()

        summaries = [
            ItemSummary(
                id=item.id,
                source=item.source,
                kind=item.kind,
                author=item.author,
                ingested_at=item.ingested_at.isoformat(),
                extracted_chars=chars_value,
                has_embedding=bool(emb),
                status=_classification_status(
                    tag_count=tags_value,
                    llm_tags=llm_value,
                    has_extraction=chars_value is not None,
                    open_failures=fail_value,
                ),
                tag_count=tags_value,
            )
            for item, chars_value, tags_value, llm_value, fail_value, emb in rows
        ]

    if status:
        summaries = [s for s in summaries if s.status == status]

    return ItemPage(
        items=summaries[offset : offset + limit],
        total=len(summaries),
        limit=limit,
        offset=offset,
    )


class ItemTagView(BaseModel):
    label: str
    slug: str
    origin: str
    confidence: float
    assigned_by: str
    #: Null for rule-based assignments — a fallback has no model call to trace.
    trace_id: str | None


class ItemDetail(BaseModel):
    """Full item. This *does* carry personal content, hence loopback-only."""

    id: uuid.UUID
    source: str
    source_id: str
    kind: str
    author: str | None
    url: str | None
    published_at: str | None
    ingested_at: str
    text: str | None
    extractor: str | None
    language: str | None
    has_embedding: bool
    embedding_model: str | None
    tags: list[ItemTagView]


@router.get(
    "/items/{item_id}",
    response_model=ItemDetail,
    dependencies=[Depends(require_internal_token)],
)
def get_item(item_id: uuid.UUID) -> ItemDetail:
    from sqlalchemy import select

    with session_scope() as session:
        item = session.get(Item, item_id)
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="item not found")

        extraction = session.execute(
            select(Extraction).where(Extraction.item_id == item_id).limit(1)
        ).scalar_one_or_none()
        embedding = session.execute(
            select(Embedding).where(Embedding.item_id == item_id)
        ).scalar_one_or_none()
        tag_rows = session.execute(
            select(Tag, ItemTag)
            .join(ItemTag, ItemTag.tag_id == Tag.id)
            .where(ItemTag.item_id == item_id)
            .order_by(ItemTag.confidence.desc())
        ).all()

        return ItemDetail(
            id=item.id,
            source=item.source,
            source_id=item.source_id,
            kind=item.kind,
            author=item.author,
            url=item.url,
            published_at=item.published_at.isoformat() if item.published_at else None,
            ingested_at=item.ingested_at.isoformat(),
            text=extraction.text if extraction else None,
            extractor=extraction.extractor if extraction else None,
            language=extraction.language if extraction else None,
            has_embedding=embedding is not None,
            embedding_model=embedding.model if embedding else None,
            tags=[
                ItemTagView(
                    label=tag.label,
                    slug=tag.slug,
                    origin=tag.origin,
                    confidence=link.confidence,
                    assigned_by=link.assigned_by,
                    trace_id=link.trace_id,
                )
                for tag, link in tag_rows
            ],
        )


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #


class SearchHitView(BaseModel):
    item_id: uuid.UUID
    score: float
    route: str
    distance: float | None
    graph_depth: int | None
    matched_tags: int | None
    source: str
    author: str | None
    ingested_at: str
    preview_chars: int | None


class SearchResponse(BaseModel):
    query_chars: int
    seed_count: int
    expanded_count: int
    tags_walked: int
    hits: list[SearchHitView]


@router.get(
    "/search",
    response_model=SearchResponse,
    dependencies=[Depends(require_internal_token)],
)
def search_items(
    q: str = Query(min_length=1),
    limit: int = Query(default=20, ge=1, le=100),
) -> SearchResponse:
    """Vector-seeded, graph-expanded retrieval.

    Delegates to ``catchment.retrieval``; no vector maths happens here.
    """
    from sqlalchemy import func, select

    from catchment.retrieval import search as run_search

    try:
        with session_scope() as session:
            result = run_search(
                q,
                items=ItemRepository(session),
                tags=TagRepository(session),
                embedder=get_embedder(),
                limit=limit,
            )

            ids = [hit.item_id for hit in result.hits]
            rows = (
                session.execute(select(Item).where(Item.id.in_(ids))).scalars().all()
                if ids
                else []
            )
            by_id = {item.id: item for item in rows}
            chars = {
                item_id: value
                for item_id, value in session.execute(
                    select(Extraction.item_id, func.length(Extraction.text)).where(
                        Extraction.item_id.in_(ids)
                    )
                ).all()
            } if ids else {}

            hits = [
                SearchHitView(
                    item_id=hit.item_id,
                    score=hit.score,
                    route=hit.route,
                    distance=hit.distance,
                    graph_depth=hit.graph_depth,
                    matched_tags=hit.matched_tags,
                    source=by_id[hit.item_id].source,
                    author=by_id[hit.item_id].author,
                    ingested_at=by_id[hit.item_id].ingested_at.isoformat(),
                    preview_chars=chars.get(hit.item_id),
                )
                for hit in result.hits
                if hit.item_id in by_id
            ]
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error)) from None
    except EmbeddingError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="embedder unavailable"
        ) from None

    return SearchResponse(
        query_chars=len(q.strip()),
        seed_count=result.seed_count,
        expanded_count=result.expanded_count,
        tags_walked=result.tags_walked,
        hits=hits,
    )


# --------------------------------------------------------------------------- #
# Review queue
# --------------------------------------------------------------------------- #


class ProposalView(BaseModel):
    id: uuid.UUID
    kind: str
    rationale: str | None
    proposed_by: str
    created_at: str
    payload: dict[str, Any]


@router.get(
    "/proposals",
    response_model=list[ProposalView],
    dependencies=[Depends(require_internal_token)],
)
def list_proposals() -> list[ProposalView]:
    with session_scope() as session:
        return [
            ProposalView(
                id=p.id,
                kind=p.kind,
                rationale=p.rationale,
                proposed_by=p.proposed_by,
                created_at=p.created_at.isoformat(),
                payload=p.payload,
            )
            for p in TaxonomyProposalRepository(session).list_pending()
        ]


# --------------------------------------------------------------------------- #
# Failures and ops
# --------------------------------------------------------------------------- #


class FailureView(BaseModel):
    id: uuid.UUID
    item_id: uuid.UUID
    stage: str
    error_type: str
    detail: str | None
    occurred_at: str


@router.get(
    "/failures",
    response_model=list[FailureView],
    dependencies=[Depends(require_internal_token)],
)
def list_failures() -> list[FailureView]:
    with session_scope() as session:
        return [
            FailureView(
                id=f.id,
                item_id=f.item_id,
                stage=f.stage,
                error_type=f.error_type,
                detail=f.detail,
                occurred_at=f.occurred_at.isoformat(),
            )
            for f in PipelineFailureRepository(session).list_open()
        ]


class ConnectorView(BaseModel):
    source: str
    last_success_at: str | None
    last_attempt_at: str
    last_outcome: str
    detail: str | None
    items_seen: int
    items_created: int
    stale: bool
    #: Threshold used to judge staleness, so the UI need not hard-code cadence.
    stale_after_seconds: int


#: Per-source staleness thresholds. WhatsApp is webhook-driven and irregular by
#: nature, so it gets a generous window; IMAP is polled and should report often.
STALE_AFTER: Final[dict[str, int]] = {
    "whatsapp": 7 * 24 * 3600,
    "email": 6 * 3600,
    "substack": 24 * 3600,
    "x": 24 * 3600,
}
DEFAULT_STALE_AFTER: Final[int] = 24 * 3600


@router.get(
    "/connectors/health",
    response_model=list[ConnectorView],
    dependencies=[Depends(require_internal_token)],
)
def connector_health() -> list[ConnectorView]:
    """Last time each source reported in.

    Distinct from item freshness: a duplicate WhatsApp delivery or a poll that
    found nothing new is a healthy round trip that creates no item.
    """
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    with session_scope() as session:
        rows = ConnectorHealthRepository(session).list_all()
        return [
            ConnectorView(
                source=row.source,
                last_success_at=(
                    row.last_success_at.isoformat() if row.last_success_at else None
                ),
                last_attempt_at=row.last_attempt_at.isoformat(),
                last_outcome=row.last_outcome,
                detail=row.detail,
                items_seen=row.items_seen,
                items_created=row.items_created,
                stale=_is_stale(row, now=now),
                stale_after_seconds=STALE_AFTER.get(row.source, DEFAULT_STALE_AFTER),
            )
            for row in rows
        ]


def _is_stale(row: Any, *, now: Any) -> bool:
    """A source that has never succeeded, or not succeeded recently enough.

    Typed structurally: it reads only ``source`` and ``last_success_at``, so a
    test can pass a stand-in without constructing an ORM row.
    """
    if row.last_success_at is None:
        return True
    threshold = STALE_AFTER.get(row.source, DEFAULT_STALE_AFTER)
    elapsed: float = (now - row.last_success_at).total_seconds()
    return elapsed > threshold
