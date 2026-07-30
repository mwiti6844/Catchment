"""Read model for the tag graph explorer.

Separate from ``repositories.py`` for two reasons: that module is already at
the size where things get hard to find, and everything here is read-only
projection for a UI rather than the write path the pipeline depends on.

It reads through ``TagRepository`` for the walks themselves. That is
deliberate — the depth-bounded recursive CTEs required by ``CLAUDE.md`` exist
in exactly one place, and a second traversal written here would be a second
place to get the bound wrong.

The layout decision lives in the data. Each node carries a *signed* ``level``:
negative for broader tags, zero for the seed, positive for narrower ones. The
taxonomy is a DAG, and broader-to-narrower reads far better as columns than as
a force-directed cloud, so the API hands the client the column index rather
than making it re-derive direction from the edge list.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from catchment.storage.models import ItemTag, Tag, TagEdge
from catchment.storage.repositories import (
    DEFAULT_TAG_DEPTH,
    TagRef,
    TagRepository,
    UnboundedTraversalError,
)

#: Ceiling on nodes in one neighbourhood. A single tag can accumulate hundreds
#: of children; drawing them all produces an unreadable page and a large
#: response for no gain. Nearest tags are kept, so truncation removes the
#: periphery rather than the structure around the seed.
MAX_GRAPH_NODES: Final[int] = 150

#: Ceiling on the seed list. It exists to pick a starting tag, not to be a
#: complete export of the taxonomy.
MAX_TAG_LIST: Final[int] = 500


@dataclass(frozen=True, slots=True)
class TagSummary:
    """One row of the tag list that seeds the explorer.

    ``parent_count`` and ``child_count`` are how you spot a tag worth opening:
    zero of both means the classifier coined it and never placed it.
    """

    tag_id: uuid.UUID
    slug: str
    label: str
    status: str
    origin: str
    item_count: int
    parent_count: int
    child_count: int


@dataclass(frozen=True, slots=True)
class TagNode:
    """A tag in a neighbourhood, with its signed distance from the seed."""

    tag_id: uuid.UUID
    slug: str
    label: str
    status: str
    origin: str
    item_count: int
    #: Hops from the seed: negative broader, 0 the seed itself, positive
    #: narrower. Doubles as the column index in a layered layout.
    level: int


@dataclass(frozen=True, slots=True)
class TagEdgeRef:
    parent_id: uuid.UUID
    child_id: uuid.UUID
    relation: str


@dataclass(frozen=True, slots=True)
class TagNeighbourhood:
    """What the explorer draws for one seed tag."""

    root: TagNode
    depth: int
    nodes: list[TagNode]
    edges: list[TagEdgeRef]
    #: True when nodes were dropped to stay under ``MAX_GRAPH_NODES``. Surfaced
    #: rather than silent: a partial graph that looks complete is misleading in
    #: exactly the way this page is supposed to prevent.
    truncated: bool


def _item_count() -> ColumnElement[int]:
    """Items carrying the tag in the enclosing query.

    Correlated subqueries rather than outer joins: joining items and edges in
    one statement multiplies the rows together, so each count comes back
    inflated by the size of the other relationships.
    """
    return (
        select(func.count())
        .select_from(ItemTag)
        .where(ItemTag.tag_id == Tag.id)
        .scalar_subquery()
    )


def _parent_count() -> ColumnElement[int]:
    """Edges where the tag is the *child* — i.e. tags broader than it."""
    return (
        select(func.count())
        .select_from(TagEdge)
        .where(TagEdge.child_id == Tag.id)
        .scalar_subquery()
    )


def _child_count() -> ColumnElement[int]:
    return (
        select(func.count())
        .select_from(TagEdge)
        .where(TagEdge.parent_id == Tag.id)
        .scalar_subquery()
    )


class TagGraphRepository:
    """Projections of the tag graph for an administrative interface."""

    def __init__(self, session: Session, *, max_depth: int = DEFAULT_TAG_DEPTH) -> None:
        self._session = session
        self._tags = TagRepository(session, max_depth=max_depth)
        self._max_depth = max_depth

    def list_tags(self, *, limit: int = 200) -> list[TagSummary]:
        """Every tag with its item and edge counts, busiest first.

        Unplaced tags are included deliberately. A tag with no items and no
        edges is not noise to be filtered — it is the classifier having coined
        something it never used again, which is worth seeing.
        """
        bounded = max(1, min(limit, MAX_TAG_LIST))

        items = _item_count()
        parents = _parent_count()
        children = _child_count()

        stmt = (
            select(Tag, items, parents, children)
            .order_by(items.desc(), Tag.label)
            .limit(bounded)
        )
        return [
            TagSummary(
                tag_id=tag.id,
                slug=tag.slug,
                label=tag.label,
                status=tag.status,
                origin=tag.origin,
                item_count=item_count,
                parent_count=parent_count,
                child_count=child_count,
            )
            for tag, item_count, parent_count, child_count in self._session.execute(stmt)
        ]

    def neighbourhood(
        self, tag_id: uuid.UUID, *, depth: int = 2
    ) -> TagNeighbourhood | None:
        """Tags within ``depth`` hops of ``tag_id``, and the edges among them.

        Returns ``None`` for an unknown tag rather than an empty graph: a stale
        link should read as "gone", not as "isolated".
        """
        if depth < 1 or depth > self._max_depth:
            raise UnboundedTraversalError(
                f"depth must be between 1 and {self._max_depth}, got {depth}"
            )

        root_tag = self._session.get(Tag, tag_id)
        if root_tag is None:
            return None

        levels = self._levels(tag_id, depth=depth)
        kept, truncated = self._bound(levels, root_id=tag_id)

        nodes = self._load_nodes(kept)
        root = next(node for node in nodes if node.tag_id == tag_id)
        return TagNeighbourhood(
            root=root,
            depth=depth,
            nodes=nodes,
            edges=self._edges(list(kept)),
            truncated=truncated,
        )

    # ----------------------------------------------------------------- #
    # Internals
    # ----------------------------------------------------------------- #

    def _levels(self, tag_id: uuid.UUID, *, depth: int) -> dict[uuid.UUID, int]:
        """Signed distance to every tag reachable from the seed.

        A diamond reaches the same tag by two routes; the shorter one wins, so
        each tag lands in exactly one column. A tag reachable in both
        directions means a cycle slipped past ``link_broader`` — the nearer
        reading is used and the graph still renders, because a broken invariant
        should be visible in the explorer rather than crash it.
        """
        levels: dict[uuid.UUID, int] = {tag_id: 0}

        def place(refs: Sequence[TagRef], sign: int) -> None:
            for ref in refs:
                level = sign * ref.depth
                current = levels.get(ref.tag_id)
                if current is None or abs(level) < abs(current):
                    levels[ref.tag_id] = level

        place(self._tags.ancestors(tag_id, max_depth=depth), -1)
        place(self._tags.descendants(tag_id, max_depth=depth), 1)
        levels[tag_id] = 0
        return levels

    def _bound(
        self, levels: dict[uuid.UUID, int], *, root_id: uuid.UUID
    ) -> tuple[dict[uuid.UUID, int], bool]:
        """Trim to ``MAX_GRAPH_NODES``, nearest first, always keeping the seed."""
        if len(levels) <= MAX_GRAPH_NODES:
            return levels, False

        ordered = sorted(levels.items(), key=lambda pair: (abs(pair[1]), str(pair[0])))
        kept = dict(ordered[:MAX_GRAPH_NODES])
        kept[root_id] = 0
        return kept, True

    def _load_nodes(self, levels: dict[uuid.UUID, int]) -> list[TagNode]:
        """Fetch tag rows and item counts for the selected ids in one query."""
        stmt = select(Tag, _item_count()).where(Tag.id.in_(list(levels)))

        nodes = [
            TagNode(
                tag_id=tag.id,
                slug=tag.slug,
                label=tag.label,
                status=tag.status,
                origin=tag.origin,
                item_count=item_count,
                level=levels[tag.id],
            )
            for tag, item_count in self._session.execute(stmt)
        ]
        return sorted(nodes, key=lambda node: (node.level, node.label))

    def _edges(self, tag_ids: Sequence[uuid.UUID]) -> list[TagEdgeRef]:
        """Edges with *both* ends in the node set.

        An edge to a tag that was not returned draws a line to nowhere, so the
        filter is on both columns rather than one.
        """
        if not tag_ids:
            return []

        ids = list(tag_ids)
        stmt = select(TagEdge).where(
            and_(TagEdge.parent_id.in_(ids), TagEdge.child_id.in_(ids))
        )
        return [
            TagEdgeRef(
                parent_id=edge.parent_id,
                child_id=edge.child_id,
                relation=edge.relation,
            )
            for edge in self._session.execute(stmt).scalars()
        ]
