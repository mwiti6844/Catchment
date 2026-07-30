"""Applying an approved merge — the one operation that rewrites history.

Every other write in this system is additive: an item arrives, a tag is coined,
an assignment is made. A merge moves assignments that already exist across
every item that carried the tag, which is why it is proposed, reviewed, and only
then executed. These tests run against real SQL because the guarantees that
matter here (one transaction, idempotent, confidence preserved) are guarantees
about the database, not about Python.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from catchment.storage.models import Item, ItemTag, Tag, TagEdge
from catchment.storage.repositories import (
    ProposalNotApprovedError,
    TagRepository,
    TaxonomyProposalRepository,
)
from catchment.taxonomy.apply import apply_approved_proposals

pytestmark = pytest.mark.integration


@pytest.fixture
def session(db_session: Session) -> Iterator[Session]:
    yield db_session


def make_tag(session: Session, slug: str) -> Tag:
    tag = Tag(slug=slug, label=slug.replace("-", " ").title(), origin="llm")
    session.add(tag)
    session.flush()
    return tag


def make_item(session: Session, source_id: str) -> Item:
    item = Item(source="whatsapp", source_id=source_id, kind="text")
    session.add(item)
    session.flush()
    return item


def assign(session: Session, item: Item, tag: Tag, confidence: float) -> None:
    session.add(
        ItemTag(item_id=item.id, tag_id=tag.id, confidence=confidence, assigned_by="llm")
    )
    session.flush()


def confidences(session: Session, tag_id: uuid.UUID) -> dict[uuid.UUID, float]:
    rows = session.execute(
        select(ItemTag.item_id, ItemTag.confidence).where(ItemTag.tag_id == tag_id)
    ).all()
    return {row[0]: row[1] for row in rows}


# --------------------------------------------------------------------------- #
# The merge itself
# --------------------------------------------------------------------------- #


def test_assignments_move_to_the_target(session: Session) -> None:
    tags = TagRepository(session)
    ml_ops, mlops = make_tag(session, "ml-ops"), make_tag(session, "mlops")
    item = make_item(session, "m1")
    assign(session, item, ml_ops, 0.8)

    tags.merge_into(source_ids=[ml_ops.id], target_id=mlops.id)

    assert confidences(session, mlops.id) == {item.id: 0.8}
    assert confidences(session, ml_ops.id) == {}, "the source keeps no assignments"


def test_the_higher_confidence_survives_a_collision(session: Session) -> None:
    """An item carrying both tags must not lose the stronger signal."""
    tags = TagRepository(session)
    source, target = make_tag(session, "ml-ops"), make_tag(session, "mlops")
    item = make_item(session, "m1")
    assign(session, item, source, 0.9)
    assign(session, item, target, 0.4)

    tags.merge_into(source_ids=[source.id], target_id=target.id)

    assert confidences(session, target.id) == {item.id: 0.9}


def test_a_weaker_source_does_not_overwrite_a_stronger_target(session: Session) -> None:
    tags = TagRepository(session)
    source, target = make_tag(session, "ml-ops"), make_tag(session, "mlops")
    item = make_item(session, "m1")
    assign(session, item, source, 0.3)
    assign(session, item, target, 0.95)

    tags.merge_into(source_ids=[source.id], target_id=target.id)

    assert confidences(session, target.id) == {item.id: 0.95}


def test_the_source_tag_is_retained_not_deleted(session: Session) -> None:
    """Old assignments stay interpretable; a deleted tag would orphan them."""
    tags = TagRepository(session)
    source, target = make_tag(session, "ml-ops"), make_tag(session, "mlops")

    tags.merge_into(source_ids=[source.id], target_id=target.id)
    session.expire_all()

    merged = session.get_one(Tag, source.id)
    assert merged.status == "merged"
    assert merged.merged_into_id == target.id


def test_a_merged_tag_leaves_the_candidate_list(session: Session) -> None:
    """Offering a merged tag back to the classifier would rebuild the duplicate."""
    tags = TagRepository(session)
    source, target = make_tag(session, "ml-ops"), make_tag(session, "mlops")
    item = make_item(session, "m1")
    assign(session, item, source, 0.8)

    tags.merge_into(source_ids=[source.id], target_id=target.id)

    assert tags.labels_for_items([item.id]) == [target.label]


def test_edges_are_repointed(session: Session) -> None:
    tags = TagRepository(session)
    parent = make_tag(session, "engineering")
    source, target = make_tag(session, "ml-ops"), make_tag(session, "mlops")
    child = make_tag(session, "model-monitoring")
    tags.add_edge(parent_id=parent.id, child_id=source.id)
    tags.add_edge(parent_id=source.id, child_id=child.id)

    tags.merge_into(source_ids=[source.id], target_id=target.id)

    assert {ref.tag_id for ref in tags.ancestors(child.id)} >= {target.id, parent.id}
    assert {ref.tag_id for ref in tags.descendants(parent.id)} >= {target.id, child.id}


def test_repointing_an_edge_never_creates_a_self_loop(session: Session) -> None:
    """The target already being the source's parent is the case that breaks it:
    repointing blindly would write target -> target, which the DB refuses."""
    tags = TagRepository(session)
    source, target = make_tag(session, "ml-ops"), make_tag(session, "mlops")
    tags.add_edge(parent_id=target.id, child_id=source.id)

    tags.merge_into(source_ids=[source.id], target_id=target.id)
    session.flush()

    edges = session.execute(select(TagEdge.parent_id, TagEdge.child_id)).all()
    assert all(row[0] != row[1] for row in edges)


def test_merging_a_tag_into_itself_is_refused(session: Session) -> None:
    tags = TagRepository(session)
    tag = make_tag(session, "mlops")

    with pytest.raises(ValueError, match="into itself"):
        tags.merge_into(source_ids=[tag.id], target_id=tag.id)


def test_merging_several_sources_at_once(session: Session) -> None:
    tags = TagRepository(session)
    a, b = make_tag(session, "ml-ops"), make_tag(session, "ml_ops")
    target = make_tag(session, "mlops")
    first, second = make_item(session, "m1"), make_item(session, "m2")
    assign(session, first, a, 0.7)
    assign(session, second, b, 0.6)

    stats = tags.merge_into(source_ids=[a.id, b.id], target_id=target.id)

    assert set(confidences(session, target.id)) == {first.id, second.id}
    assert stats.tags_merged == 2
    assert stats.assignments_moved == 2


# --------------------------------------------------------------------------- #
# The job that consumes the review queue
# --------------------------------------------------------------------------- #


def approved_merge(
    session: Session, *, sources: list[Tag], target: Tag
) -> uuid.UUID:
    proposals = TaxonomyProposalRepository(session)
    proposal = proposals.propose_merge(
        source_tag_ids=[t.id for t in sources], target_tag_id=target.id
    )
    proposals.approve(proposal.id, reviewer="david")
    return proposal.id


def test_an_approved_proposal_is_applied(session: Session) -> None:
    source, target = make_tag(session, "ml-ops"), make_tag(session, "mlops")
    item = make_item(session, "m1")
    assign(session, item, source, 0.8)
    proposal_id = approved_merge(session, sources=[source], target=target)

    applied = apply_approved_proposals(session=session)

    assert [outcome.proposal_id for outcome in applied] == [proposal_id]
    assert confidences(session, target.id) == {item.id: 0.8}
    assert session.get_one(Tag, source.id).status == "merged"


def test_applying_is_idempotent(session: Session) -> None:
    """A job retried after a partial failure must not double-apply."""
    source, target = make_tag(session, "ml-ops"), make_tag(session, "mlops")
    item = make_item(session, "m1")
    assign(session, item, source, 0.8)
    approved_merge(session, sources=[source], target=target)

    apply_approved_proposals(session=session)
    second_run = apply_approved_proposals(session=session)

    assert second_run == [], "an applied proposal must not be picked up again"
    assert confidences(session, target.id) == {item.id: 0.8}


def test_a_pending_proposal_is_left_alone(session: Session) -> None:
    """Approval is the gate. Without it nothing may touch the graph."""
    proposals = TaxonomyProposalRepository(session)
    source, target = make_tag(session, "ml-ops"), make_tag(session, "mlops")
    item = make_item(session, "m1")
    assign(session, item, source, 0.8)
    proposals.propose_merge(source_tag_ids=[source.id], target_tag_id=target.id)

    assert apply_approved_proposals(session=session) == []
    assert confidences(session, source.id) == {item.id: 0.8}
    assert session.get_one(Tag, source.id).status == "active"


def test_a_rejected_proposal_is_never_applied(session: Session) -> None:
    proposals = TaxonomyProposalRepository(session)
    source, target = make_tag(session, "ml-ops"), make_tag(session, "mlops")
    proposal = proposals.propose_merge(
        source_tag_ids=[source.id], target_tag_id=target.id
    )
    proposals.reject(proposal.id, reviewer="david")

    assert apply_approved_proposals(session=session) == []
    assert session.get_one(Tag, source.id).status == "active"


def test_a_proposal_naming_a_vanished_tag_fails_without_blocking_others(
    session: Session,
) -> None:
    """One unapplicable proposal must not stall the queue behind it."""
    ghost = uuid.uuid4()
    proposals = TaxonomyProposalRepository(session)
    broken = proposals.propose_merge(source_tag_ids=[ghost], target_tag_id=ghost)
    proposals.approve(broken.id, reviewer="david")

    source, target = make_tag(session, "ml-ops"), make_tag(session, "mlops")
    item = make_item(session, "m1")
    assign(session, item, source, 0.8)
    good_id = approved_merge(session, sources=[source], target=target)

    applied = apply_approved_proposals(session=session)

    assert [outcome.proposal_id for outcome in applied] == [good_id]
    assert session.get_one(Tag, source.id).status == "merged"


def test_mark_applied_still_refuses_an_unapproved_proposal(session: Session) -> None:
    """The invariant the executor depends on."""
    proposals = TaxonomyProposalRepository(session)
    tag = make_tag(session, "ml-ops")
    proposal = proposals.propose_merge(source_tag_ids=[tag.id], target_tag_id=tag.id)

    with pytest.raises(ProposalNotApprovedError):
        proposals.mark_applied(proposal.id)
