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
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from catchment.config import MissingConfiguration, Settings, get_settings
from catchment.logging_config import get_logger, log_context
from catchment.storage.db import session_scope
from catchment.storage.repositories import (
    RepositoryError,
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
