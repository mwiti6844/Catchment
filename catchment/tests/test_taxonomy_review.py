"""Taxonomy merges and splits are proposed, never auto-executed."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from catchment.storage.models import TaxonomyProposal
from catchment.storage.repositories import (
    ProposalNotApprovedError,
    RepositoryError,
    TaxonomyProposalRepository,
)


class FakeSession:
    """Minimal stand-in exercising the guards that run before any SQL."""

    def __init__(self, stored: dict[uuid.UUID, TaxonomyProposal] | None = None) -> None:
        self.stored = stored or {}
        self.added: list[Any] = []
        self.flushes = 0

    def add(self, obj: Any) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        self.added.append(obj)
        self.stored[obj.id] = obj

    def flush(self) -> None:
        self.flushes += 1

    def get(self, _model: type, obj_id: uuid.UUID) -> Any:
        return self.stored.get(obj_id)


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def repo(session: FakeSession) -> TaxonomyProposalRepository:
    return TaxonomyProposalRepository(session)  # type: ignore[arg-type]


def test_proposed_merge_lands_pending_and_changes_nothing(
    repo: TaxonomyProposalRepository, session: FakeSession
) -> None:
    source, target = uuid.uuid4(), uuid.uuid4()

    proposal = repo.propose_merge(
        source_tag_ids=[source], target_tag_id=target, rationale="near-duplicate"
    )

    assert proposal.kind == "merge"
    assert proposal.status == "pending"
    assert proposal.applied_at is None
    assert proposal.reviewed_by is None
    assert proposal.payload["target_tag_id"] == str(target)
    # The only write is the queue row itself.
    assert session.added == [proposal]


def test_proposed_split_lands_pending(repo: TaxonomyProposalRepository) -> None:
    proposal = repo.propose_split(
        tag_id=uuid.uuid4(), into=[{"label": "LLM evals"}, {"label": "LLM tracing"}]
    )
    assert proposal.kind == "split"
    assert proposal.status == "pending"
    assert len(proposal.payload["into"]) == 2


def test_pending_proposal_cannot_be_applied(
    repo: TaxonomyProposalRepository, session: FakeSession
) -> None:
    proposal = repo.propose_merge(
        source_tag_ids=[uuid.uuid4()], target_tag_id=uuid.uuid4()
    )

    with pytest.raises(ProposalNotApprovedError, match="only approved"):
        repo.mark_applied(proposal.id)

    assert session.stored[proposal.id].status == "pending"


def test_rejected_proposal_cannot_be_applied(
    repo: TaxonomyProposalRepository, session: FakeSession
) -> None:
    proposal = repo.propose_merge(
        source_tag_ids=[uuid.uuid4()], target_tag_id=uuid.uuid4()
    )
    proposal.status = "rejected"
    proposal.reviewed_by = "david"

    with pytest.raises(ProposalNotApprovedError):
        repo.mark_applied(proposal.id)


def test_approved_proposal_can_be_applied(
    repo: TaxonomyProposalRepository, session: FakeSession
) -> None:
    proposal = repo.propose_merge(
        source_tag_ids=[uuid.uuid4()], target_tag_id=uuid.uuid4()
    )
    proposal.status = "approved"
    proposal.reviewed_by = "david"

    applied = repo.mark_applied(proposal.id)

    assert applied.status == "applied"
    assert applied.applied_at is not None


def test_unknown_proposal_is_an_error(repo: TaxonomyProposalRepository) -> None:
    with pytest.raises(RepositoryError, match="does not exist"):
        repo.mark_applied(uuid.uuid4())


@pytest.mark.parametrize("reviewer", ["", "   "])
def test_decision_requires_a_reviewer_identity(
    repo: TaxonomyProposalRepository, reviewer: str
) -> None:
    with pytest.raises(RepositoryError, match="reviewer identity"):
        repo.approve(uuid.uuid4(), reviewer=reviewer)
    with pytest.raises(RepositoryError, match="reviewer identity"):
        repo.reject(uuid.uuid4(), reviewer=reviewer)
