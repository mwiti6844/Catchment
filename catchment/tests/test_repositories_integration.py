from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from catchment.storage.models import Item, Tag
from catchment.storage.repositories import (
    ItemRepository,
    TagRepository,
    TaxonomyProposalRepository,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def session(db_session: Session) -> Iterator[Session]:
    """Alias for the shared transactional session in conftest.py."""
    yield db_session


def test_duplicate_source_id_is_rejected_by_the_database(session: Session) -> None:
    session.add(Item(source="whatsapp", source_id="m1", kind="text"))
    session.flush()
    session.add(Item(source="whatsapp", source_id="m1", kind="text"))

    with pytest.raises(IntegrityError):
        session.flush()


def test_upsert_is_idempotent(session: Session) -> None:
    repo = ItemRepository(session)

    first, created_first = repo.upsert(source="x", source_id="t1", kind="link")
    second, created_second = repo.upsert(source="x", source_id="t1", kind="link")

    assert created_first is True
    assert created_second is False
    assert first.id == second.id


def test_extraction_rerun_replaces_previous_output(session: Session) -> None:
    repo = ItemRepository(session)
    item, _ = repo.upsert(source="email", source_id="e1", kind="text")

    repo.add_extraction(item_id=item.id, extractor="whisper", text="first pass")
    second = repo.add_extraction(item_id=item.id, extractor="whisper", text="second pass")

    assert second.text == "second pass"
    assert len(repo.get(item.id).extractions) == 1  # type: ignore[union-attr]


def test_embedding_roundtrip_and_nearest(session: Session) -> None:
    repo = ItemRepository(session)
    near, _ = repo.upsert(source="x", source_id="near", kind="link")
    far, _ = repo.upsert(source="x", source_id="far", kind="link")

    repo.set_embedding(item_id=near.id, model="bge-m3", vector=[1.0] + [0.0] * 1023)
    repo.set_embedding(item_id=far.id, model="bge-m3", vector=[0.0] * 1023 + [1.0])
    session.flush()

    results = repo.nearest(vector=[1.0] + [0.0] * 1023, limit=2)

    assert results[0][0].id == near.id
    assert results[0][1] < results[1][1]


def test_tag_walk_stops_at_the_depth_bound(session: Session) -> None:
    repo = TagRepository(session, max_depth=8)
    chain = []
    for level in range(6):
        tag, _ = repo.get_or_create(slug=f"level-{level}", label=f"Level {level}")
        chain.append(tag)
    for parent, child in zip(chain, chain[1:], strict=False):
        repo.add_edge(parent_id=parent.id, child_id=child.id)
    session.flush()

    deepest = chain[-1]
    assert len(repo.ancestors(deepest.id)) == 5
    assert len(repo.ancestors(deepest.id, max_depth=2)) == 2
    assert len(repo.descendants(chain[0].id, max_depth=3)) == 3


def test_tag_graph_cycle_does_not_hang(session: Session) -> None:
    """A cycle must terminate at the bound rather than looping forever."""
    repo = TagRepository(session, max_depth=4)
    a, _ = repo.get_or_create(slug="cycle-a", label="A")
    b, _ = repo.get_or_create(slug="cycle-b", label="B")
    repo.add_edge(parent_id=a.id, child_id=b.id)
    repo.add_edge(parent_id=b.id, child_id=a.id)
    session.flush()

    walked = repo.ancestors(a.id)

    assert len(walked) == 4
    assert max(ref.depth for ref in walked) == 4


def test_self_loop_is_rejected(session: Session) -> None:
    repo = TagRepository(session)
    tag, _ = repo.get_or_create(slug="solo", label="Solo")
    session.flush()

    # add_edge issues its INSERT immediately, so the constraint fires here
    # rather than at the next flush.
    with pytest.raises(IntegrityError):
        repo.add_edge(parent_id=tag.id, child_id=tag.id)


def test_approval_flow_records_the_reviewer(session: Session) -> None:
    tags = TagRepository(session)
    source, _ = tags.get_or_create(slug="ml-ops", label="MLOps")
    target, _ = tags.get_or_create(slug="mlops", label="MLOps")
    session.flush()

    proposals = TaxonomyProposalRepository(session)
    proposal = proposals.propose_merge(
        source_tag_ids=[source.id], target_tag_id=target.id, rationale="same concept"
    )
    session.flush()

    assert [p.id for p in proposals.list_pending()] == [proposal.id]

    approved = proposals.approve(proposal.id, reviewer="david")
    assert approved.status == "approved"
    assert approved.reviewed_by == "david"
    assert approved.reviewed_at is not None
    # Approval alone leaves the graph untouched.
    assert session.get(Tag, source.id) is not None

    applied = proposals.mark_applied(proposal.id)
    assert applied.status == "applied"
    assert proposals.list_pending() == []


def test_a_decided_proposal_cannot_be_decided_again(session: Session) -> None:
    proposals = TaxonomyProposalRepository(session)
    proposal = proposals.propose_merge(
        source_tag_ids=[uuid.uuid4()], target_tag_id=uuid.uuid4()
    )
    session.flush()

    proposals.reject(proposal.id, reviewer="david")
    with pytest.raises(Exception, match="not pending"):
        proposals.approve(proposal.id, reviewer="david")


# --------------------------------------------------------------------------- #
# Placing tags in the graph
# --------------------------------------------------------------------------- #


def _tag(session: Session, slug: str) -> Tag:
    tag = Tag(slug=slug, label=slug.title(), origin="llm")
    session.add(tag)
    session.flush()
    return tag


def test_link_broader_creates_an_edge(session: Session) -> None:
    tags = TagRepository(session)
    parent, child = _tag(session, "machine-learning"), _tag(session, "rag")

    assert tags.link_broader(parent_id=parent.id, child_id=child.id) is True
    assert [ref.tag_id for ref in tags.ancestors(child.id)] == [parent.id]


def test_link_broader_refuses_an_edge_that_would_close_a_cycle(session: Session) -> None:
    """The graph is a DAG by intent and not by enforcement. Two individually
    reasonable edges are all it takes to close a loop, so the second is refused."""
    tags = TagRepository(session)
    a, b = _tag(session, "machine-learning"), _tag(session, "rag")
    tags.link_broader(parent_id=a.id, child_id=b.id)

    assert tags.link_broader(parent_id=b.id, child_id=a.id) is False
    assert [ref.tag_id for ref in tags.ancestors(a.id)] == []


def test_link_broader_refuses_a_longer_cycle(session: Session) -> None:
    tags = TagRepository(session)
    a, b, c = _tag(session, "a"), _tag(session, "b"), _tag(session, "c")
    tags.link_broader(parent_id=a.id, child_id=b.id)
    tags.link_broader(parent_id=b.id, child_id=c.id)

    assert tags.link_broader(parent_id=c.id, child_id=a.id) is False


def test_link_broader_refuses_a_self_loop(session: Session) -> None:
    tags = TagRepository(session)
    tag = _tag(session, "solo")

    assert tags.link_broader(parent_id=tag.id, child_id=tag.id) is False


def test_link_broader_is_idempotent(session: Session) -> None:
    tags = TagRepository(session)
    parent, child = _tag(session, "machine-learning"), _tag(session, "rag")

    assert tags.link_broader(parent_id=parent.id, child_id=child.id) is True
    assert tags.link_broader(parent_id=parent.id, child_id=child.id) is True
    assert len(tags.ancestors(child.id)) == 1


def test_get_by_slug_ignores_a_merged_tag(session: Session) -> None:
    """A merged tag must not become the parent of anything new."""
    tags = TagRepository(session)
    source, target = _tag(session, "ml-ops"), _tag(session, "mlops")
    tags.merge_into(source_ids=[source.id], target_id=target.id)

    assert tags.get_by_slug("ml-ops") is None
    assert tags.get_by_slug("mlops") is not None
