"""Routes behind the tag graph explorer.

Its own module rather than more of ``internal_api.py``: these are read-only
projections of the taxonomy, and that file already carries the review gate,
the inbox and search.

The routes are mounted into ``internal_api.router``, not into the app
directly, so the test asserting that *every* internal route carries the token
dependency continues to cover them. A separately-mounted router would be a
quiet way to add an unauthenticated route.

No route here returns item text — only tag structure and counts.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from catchment.internal_auth import require_internal_token
from catchment.storage.db import session_scope
from catchment.storage.repositories import DEFAULT_TAG_DEPTH, UnboundedTraversalError
from catchment.storage.tag_graph import MAX_TAG_LIST, TagGraphRepository, TagNode

router = APIRouter()


class TagSummaryView(BaseModel):
    id: uuid.UUID
    slug: str
    label: str
    status: str
    origin: str
    item_count: int
    #: Edges in each direction. Zero of both means the classifier coined the
    #: tag and never placed it — the rows most worth opening.
    parent_count: int
    child_count: int


@router.get(
    "/tags",
    response_model=list[TagSummaryView],
    dependencies=[Depends(require_internal_token)],
)
def list_tags(
    limit: int = Query(default=200, ge=1, le=MAX_TAG_LIST),
) -> list[TagSummaryView]:
    """The whole vocabulary, busiest first, for seeding the explorer."""
    with session_scope() as session:
        return [
            TagSummaryView(
                id=row.tag_id,
                slug=row.slug,
                label=row.label,
                status=row.status,
                origin=row.origin,
                item_count=row.item_count,
                parent_count=row.parent_count,
                child_count=row.child_count,
            )
            for row in TagGraphRepository(session).list_tags(limit=limit)
        ]


class TagNodeView(BaseModel):
    id: uuid.UUID
    slug: str
    label: str
    status: str
    origin: str
    item_count: int
    #: Signed hops from the seed: negative broader, 0 the seed, positive
    #: narrower. The client uses it directly as a column index.
    level: int


class TagEdgeView(BaseModel):
    parent: uuid.UUID
    child: uuid.UUID
    relation: str


class TagGraphView(BaseModel):
    root: TagNodeView
    depth: int
    nodes: list[TagNodeView]
    edges: list[TagEdgeView]
    #: True when the neighbourhood was trimmed to stay renderable. Surfaced so
    #: a partial graph is never mistaken for the whole one.
    truncated: bool


@router.get(
    "/tags/{tag_id}/graph",
    response_model=TagGraphView,
    dependencies=[Depends(require_internal_token)],
)
def tag_graph(
    tag_id: uuid.UUID,
    depth: int = Query(default=2, ge=1, le=DEFAULT_TAG_DEPTH),
) -> TagGraphView:
    """The neighbourhood around one tag, within an explicit depth bound.

    The bound is doubly enforced: rejected here by the query constraint, and
    again in the repository, which owns the recursive walk. The route's limit
    can be relaxed without touching the guarantee.
    """
    with session_scope() as session:
        try:
            result = TagGraphRepository(session).neighbourhood(tag_id, depth=depth)
        except UnboundedTraversalError as error:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from None

        if result is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="tag not found")

        return TagGraphView(
            root=_node(result.root),
            depth=result.depth,
            nodes=[_node(node) for node in result.nodes],
            edges=[
                TagEdgeView(
                    parent=edge.parent_id, child=edge.child_id, relation=edge.relation
                )
                for edge in result.edges
            ],
            truncated=result.truncated,
        )


def _node(node: TagNode) -> TagNodeView:
    return TagNodeView(
        id=node.tag_id,
        slug=node.slug,
        label=node.label,
        status=node.status,
        origin=node.origin,
        item_count=node.item_count,
        level=node.level,
    )
