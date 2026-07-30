"""Execute approved taxonomy proposals.

The review queue was write-only until this existed: a human could approve a
merge and nothing would ever act on it. This is the consumer.

Two properties are load-bearing.

*One transaction per proposal.* A merge touches many rows across three tables.
Committing part-way would leave assignments pointing at a tag already marked
merged, which no later run could reconstruct.

*Idempotent.* A proposal is only picked up while ``approved``, and the same
transaction that performs the merge flips it to ``applied``. A job retried
after a crash either committed everything (so the proposal is no longer
approved and is skipped) or committed nothing (so it runs cleanly again).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from catchment.logging_config import get_logger, log_context
from catchment.storage.db import session_scope
from catchment.storage.models import TaxonomyProposal
from catchment.storage.repositories import (
    MergeStats,
    TagRepository,
    TaxonomyProposalRepository,
)

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AppliedProposal:
    """One proposal that was executed successfully."""

    proposal_id: uuid.UUID
    kind: str
    stats: MergeStats


class ProposalPayloadError(ValueError):
    """Raised when a proposal's payload cannot be executed as written."""


def apply_approved_proposals(
    *, session: Session | None = None, limit: int = 100
) -> list[AppliedProposal]:
    """Apply every approved proposal, returning the ones that succeeded.

    A proposal that cannot be applied — a tag deleted since approval, a payload
    that no longer parses — is logged and left ``approved`` rather than raising.
    One unapplicable proposal must not stall every proposal queued behind it,
    and leaving it approved means it is still visible for a human to resolve.
    """
    if session is not None:
        return _apply_all(session, limit=limit)

    with session_scope() as scoped:
        return _apply_all(scoped, limit=limit)


def apply_proposal(
    proposal_id: uuid.UUID, *, session: Session
) -> AppliedProposal | None:
    """Apply one approved proposal, or None if it could not be applied.

    Used by the review surface so approving in the dashboard takes effect
    immediately. The approval still happens first and is still recorded — this
    only removes the gap where an approved change sat waiting for a job nobody
    had written.
    """
    proposals = TaxonomyProposalRepository(session)
    proposal = session.get(TaxonomyProposal, proposal_id)
    if proposal is None or proposal.status != "approved":
        return None
    return _apply_one(session, proposals, TagRepository(session), proposal)


def _apply_all(session: Session, *, limit: int) -> list[AppliedProposal]:
    proposals = TaxonomyProposalRepository(session)
    tags = TagRepository(session)

    applied: list[AppliedProposal] = []
    for proposal in proposals.list_approved(limit=limit):
        outcome = _apply_one(session, proposals, tags, proposal)
        if outcome is not None:
            applied.append(outcome)
    return applied


def _apply_one(
    session: Session,
    proposals: TaxonomyProposalRepository,
    tags: TagRepository,
    proposal: TaxonomyProposal,
) -> AppliedProposal | None:
    """Execute one proposal in its own nested transaction.

    ``begin_nested`` gives each proposal a savepoint, so a failure rolls back
    that proposal alone and leaves the ones already applied in this run intact.
    """
    try:
        with session.begin_nested():
            stats = _execute(tags, proposal)
            proposals.mark_applied(proposal.id)
    except Exception as error:
        # The message may name tags; the id and type are enough to find it.
        logger.error(
            "taxonomy proposal could not be applied",
            extra=log_context(
                proposal_id=str(proposal.id),
                kind=proposal.kind,
                error_type=type(error).__name__,
            ),
        )
        return None

    logger.info(
        "taxonomy proposal applied",
        extra=log_context(
            proposal_id=str(proposal.id),
            kind=proposal.kind,
            tags_merged=stats.tags_merged,
            assignments_moved=stats.assignments_moved,
        ),
    )
    return AppliedProposal(proposal_id=proposal.id, kind=proposal.kind, stats=stats)


def _execute(tags: TagRepository, proposal: TaxonomyProposal) -> MergeStats:
    if proposal.kind == "merge":
        source_ids, target_id = _merge_payload(proposal)
        return tags.merge_into(source_ids=source_ids, target_id=target_id)

    # Splits need a human to say which items go where — the payload names the
    # new tags but not the reassignment, and guessing would be a taxonomy
    # decision made by code. Deliberately unimplemented rather than approximated.
    raise ProposalPayloadError(f"{proposal.kind!r} proposals cannot be applied yet")


def _merge_payload(
    proposal: TaxonomyProposal,
) -> tuple[list[uuid.UUID], uuid.UUID]:
    """Read ids back out of the JSON payload, failing loudly on anything odd."""
    payload = proposal.payload or {}
    raw_sources = payload.get("source_tag_ids")
    raw_target = payload.get("target_tag_id")

    if not isinstance(raw_sources, list) or not raw_sources:
        raise ProposalPayloadError("merge payload has no source_tag_ids")
    if not isinstance(raw_target, str):
        raise ProposalPayloadError("merge payload has no target_tag_id")

    try:
        sources = [uuid.UUID(value) for value in raw_sources]
        target = uuid.UUID(raw_target)
    except (AttributeError, TypeError, ValueError) as error:
        raise ProposalPayloadError("merge payload holds unreadable ids") from error

    return sources, target
