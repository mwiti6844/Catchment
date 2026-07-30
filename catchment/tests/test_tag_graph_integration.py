"""The read model behind the tag graph explorer.

The explorer's whole purpose is watching the taxonomy take shape, so the thing
under test is mostly *shape*: which tags are reachable from a seed, how far
away they are, and which edges actually connect the tags being drawn.

Two constraints are load-bearing. The walk is depth-bounded (CLAUDE.md), so a
depth that exceeds the configured bound is refused rather than quietly
widened. And the returned edge list must reference only returned nodes — an
edge pointing at a tag that was never sent draws a line to nowhere.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from catchment.storage.models import Item, ItemTag, Tag, TagEdge
from catchment.storage.repositories import UnboundedTraversalError
from catchment.storage.tag_graph import (
    MAX_GRAPH_NODES,
    TagGraphRepository,
    TagNeighbourhood,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def session(db_session: Session) -> Iterator[Session]:
    yield db_session


@pytest.fixture
def graph(session: Session) -> TagGraphRepository:
    return TagGraphRepository(session)


def make_tag(session: Session, slug: str, *, status: str = "active") -> Tag:
    tag = Tag(slug=slug, label=slug.replace("-", " ").title(), status=status)
    session.add(tag)
    session.flush()
    return tag


def link(session: Session, parent: Tag, child: Tag) -> None:
    session.add(TagEdge(parent_id=parent.id, child_id=child.id, relation="broader"))
    session.flush()


def tag_items(session: Session, tag: Tag, count: int, *, prefix: str = "") -> list[Item]:
    items = []
    for index in range(count):
        item = Item(
            source="whatsapp", source_id=f"{prefix}{tag.slug}-{index}", kind="text"
        )
        session.add(item)
        session.flush()
        session.add(ItemTag(item_id=item.id, tag_id=tag.id, confidence=0.9))
        items.append(item)
    session.flush()
    return items


def levels(neighbourhood: TagNeighbourhood) -> dict[str, int]:
    return {node.slug: node.level for node in neighbourhood.nodes}


# --------------------------------------------------------------------------- #
# Shape of the neighbourhood
# --------------------------------------------------------------------------- #


def test_an_isolated_tag_returns_only_itself(
    session: Session, graph: TagGraphRepository
) -> None:
    """A tag the classifier has coined but never placed is the common case
    early on. It must render as a lone node, not as an empty result."""
    lonely = make_tag(session, "lonely")

    result = graph.neighbourhood(lonely.id)

    assert result is not None
    assert levels(result) == {"lonely": 0}
    assert result.edges == []


def test_broader_tags_sit_above_and_narrower_below(
    session: Session, graph: TagGraphRepository
) -> None:
    """The sign of ``level`` is the direction, which is what lets the UI lay the
    graph out in columns instead of guessing at a force-directed blob."""
    science = make_tag(session, "science")
    hydrology = make_tag(session, "hydrology")
    basin = make_tag(session, "drainage-basin")
    link(session, science, hydrology)
    link(session, hydrology, basin)

    result = graph.neighbourhood(hydrology.id, depth=2)

    assert result is not None
    assert levels(result) == {"science": -1, "hydrology": 0, "drainage-basin": 1}


def test_the_depth_bound_stops_the_walk(
    session: Session, graph: TagGraphRepository
) -> None:
    root = make_tag(session, "root")
    child = make_tag(session, "child")
    grandchild = make_tag(session, "grandchild")
    link(session, root, child)
    link(session, child, grandchild)

    shallow = graph.neighbourhood(root.id, depth=1)
    deeper = graph.neighbourhood(root.id, depth=2)

    assert shallow is not None and deeper is not None
    assert "grandchild" not in levels(shallow)
    assert "grandchild" in levels(deeper)


def test_a_depth_beyond_the_configured_bound_is_refused(
    session: Session, graph: TagGraphRepository
) -> None:
    """CLAUDE.md: recursive walks carry an explicit bound. Silently clamping an
    over-large request would make the bound invisible to the caller."""
    tag = make_tag(session, "bounded")

    with pytest.raises(UnboundedTraversalError):
        graph.neighbourhood(tag.id, depth=99)


@pytest.mark.parametrize("depth", [0, -1])
def test_a_non_positive_depth_is_refused(
    session: Session, graph: TagGraphRepository, depth: int
) -> None:
    tag = make_tag(session, "bounded")

    with pytest.raises(UnboundedTraversalError):
        graph.neighbourhood(tag.id, depth=depth)


def test_an_unknown_tag_is_absent_rather_than_empty(
    graph: TagGraphRepository,
) -> None:
    """A deleted or mistyped id is a 404, not a graph with no nodes."""
    assert graph.neighbourhood(uuid.uuid4()) is None


def test_a_diamond_reports_the_shorter_path(
    session: Session, graph: TagGraphRepository
) -> None:
    """Two routes to one tag is normal in a DAG. It must appear once, at the
    level it is actually nearest by — otherwise the layout draws it twice."""
    root = make_tag(session, "root")
    left = make_tag(session, "left")
    right = make_tag(session, "right")
    shared = make_tag(session, "shared")
    link(session, root, left)
    link(session, root, right)
    link(session, root, shared)  # direct: depth 1
    link(session, left, shared)  # via left: depth 2

    result = graph.neighbourhood(root.id, depth=3)

    assert result is not None
    assert [n.slug for n in result.nodes].count("shared") == 1
    assert levels(result)["shared"] == 1


# --------------------------------------------------------------------------- #
# Edges
# --------------------------------------------------------------------------- #


def test_edges_never_reference_a_tag_that_was_not_returned(
    session: Session, graph: TagGraphRepository
) -> None:
    """An edge to a node outside the set draws a line to nowhere."""
    root = make_tag(session, "root")
    child = make_tag(session, "child")
    grandchild = make_tag(session, "grandchild")
    link(session, root, child)
    link(session, child, grandchild)

    result = graph.neighbourhood(root.id, depth=1)

    assert result is not None
    returned = {node.tag_id for node in result.nodes}
    for edge in result.edges:
        assert edge.parent_id in returned and edge.child_id in returned


def test_edges_between_two_reached_tags_are_included(
    session: Session, graph: TagGraphRepository
) -> None:
    """Sibling structure is the interesting part: an edge between two children
    is what distinguishes a hierarchy from a star."""
    root = make_tag(session, "root")
    first = make_tag(session, "first")
    second = make_tag(session, "second")
    link(session, root, first)
    link(session, root, second)
    link(session, first, second)

    result = graph.neighbourhood(root.id, depth=2)

    assert result is not None
    pairs = {(e.parent_id, e.child_id) for e in result.edges}
    assert (first.id, second.id) in pairs


# --------------------------------------------------------------------------- #
# Counts and status
# --------------------------------------------------------------------------- #


def test_item_counts_are_per_tag_and_direct(
    session: Session, graph: TagGraphRepository
) -> None:
    """Direct assignments only. Rolling descendants up would make a parent's
    count depend on where its children were placed, which is exactly the thing
    the explorer exists to let you judge."""
    parent = make_tag(session, "parent")
    child = make_tag(session, "child")
    link(session, parent, child)
    tag_items(session, parent, 2)
    tag_items(session, child, 3)

    result = graph.neighbourhood(parent.id, depth=1)

    assert result is not None
    counts = {node.slug: node.item_count for node in result.nodes}
    assert counts == {"parent": 2, "child": 3}


def test_a_merged_tag_keeps_its_status_rather_than_disappearing(
    session: Session, graph: TagGraphRepository
) -> None:
    """Seeing what a merge did to the graph is part of the point. Filtering
    merged tags out would hide the outcome of the review queue."""
    merged = make_tag(session, "gone", status="merged")

    result = graph.neighbourhood(merged.id)

    assert result is not None
    assert result.root.status == "merged"


def test_the_root_is_reported_separately(
    session: Session, graph: TagGraphRepository
) -> None:
    hydrology = make_tag(session, "hydrology")

    result = graph.neighbourhood(hydrology.id)

    assert result is not None
    assert result.root.tag_id == hydrology.id
    assert result.root.level == 0


# --------------------------------------------------------------------------- #
# Bounds
# --------------------------------------------------------------------------- #


def test_a_hub_tag_is_truncated_rather_than_returned_whole(
    session: Session, graph: TagGraphRepository
) -> None:
    """One tag can accumulate hundreds of children. Sending them all would
    render an unreadable page and a large response for no gain."""
    root = make_tag(session, "hub")
    for index in range(MAX_GRAPH_NODES + 10):
        link(session, root, make_tag(session, f"spoke-{index:03d}"))

    result = graph.neighbourhood(root.id, depth=1)

    assert result is not None
    assert len(result.nodes) <= MAX_GRAPH_NODES
    assert result.truncated is True


def test_a_small_graph_is_not_flagged_as_truncated(
    session: Session, graph: TagGraphRepository
) -> None:
    root = make_tag(session, "small")

    result = graph.neighbourhood(root.id)

    assert result is not None
    assert result.truncated is False


def test_truncation_keeps_the_root(
    session: Session, graph: TagGraphRepository
) -> None:
    """Dropping the seed would leave the explorer showing a neighbourhood of
    something that is not on screen."""
    root = make_tag(session, "hub")
    for index in range(MAX_GRAPH_NODES + 10):
        link(session, root, make_tag(session, f"spoke-{index:03d}"))

    result = graph.neighbourhood(root.id, depth=1)

    assert result is not None
    assert any(node.tag_id == root.id for node in result.nodes)


# --------------------------------------------------------------------------- #
# The tag list that seeds the explorer
# --------------------------------------------------------------------------- #


def test_the_tag_list_reports_item_and_edge_counts(
    session: Session, graph: TagGraphRepository
) -> None:
    """Edge counts are how you find a seed worth opening: a tag with no edges
    is unplaced, and one with many is a hub."""
    parent = make_tag(session, "parent")
    middle = make_tag(session, "middle")
    child = make_tag(session, "child")
    link(session, parent, middle)
    link(session, middle, child)
    tag_items(session, middle, 4)

    summary = {row.slug: row for row in graph.list_tags()}

    assert summary["middle"].item_count == 4
    assert summary["middle"].parent_count == 1
    assert summary["middle"].child_count == 1
    assert summary["parent"].parent_count == 0


def test_the_tag_list_leads_with_the_busiest_tags(
    session: Session, graph: TagGraphRepository
) -> None:
    quiet = make_tag(session, "quiet")
    busy = make_tag(session, "busy")
    tag_items(session, quiet, 1)
    tag_items(session, busy, 5)

    listed = [row.slug for row in graph.list_tags()]

    assert listed.index("busy") < listed.index("quiet")


def test_the_tag_list_is_bounded(session: Session, graph: TagGraphRepository) -> None:
    for index in range(8):
        make_tag(session, f"tag-{index}")

    assert len(graph.list_tags(limit=3)) == 3


def test_an_unplaced_tag_still_appears_in_the_list(
    session: Session, graph: TagGraphRepository
) -> None:
    """Tags with no items and no edges are the ones worth reviewing — they are
    where the classifier coined something it never used again."""
    make_tag(session, "orphan")

    assert "orphan" in {row.slug for row in graph.list_tags()}
