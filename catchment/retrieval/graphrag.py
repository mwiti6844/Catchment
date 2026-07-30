"""Vector-seeded, graph-expanded retrieval.

Pure vector search finds items whose *wording* resembles the query. That misses
the thing this system is for: an item can be squarely about a topic while
sharing little vocabulary with how you asked. The tag graph already encodes
those relationships, so retrieval runs in two stages:

1. **Seed** — embed the query, take the nearest items by cosine distance.
2. **Expand** — collect the seeds' tags, walk the graph outward within the
   depth bound, and pull in items carrying the tags reached.

Seeds rank on vector distance. Expanded items rank on how far they sit in the
graph and how many of the reached tags they carry, always below the seeds:
a direct semantic match should never be displaced by something two hops away.

Graph walks go through ``TagRepository.ancestors``/``.descendants``, which
carry the depth bound required by CLAUDE.md. There is no unbounded traversal
here, and the tag graph is not guaranteed acyclic.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Final

from catchment.classification.embeddings import Embedder
from catchment.config import Settings, get_settings
from catchment.logging_config import get_logger, log_context
from catchment.storage.repositories import ItemRepository, TagRepository

logger = get_logger(__name__)

#: Expanded hits always score below seeds. A two-hop association must not
#: outrank a direct semantic match, however many tags it happens to share.
EXPANDED_SCORE_CEILING: Final[float] = 0.5


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One result, carrying why it was retrieved."""

    item_id: uuid.UUID
    score: float
    #: ``seed`` = matched the query vector. ``expanded`` = reached through the
    #: tag graph. Surfaced so the UI can show *why* something appeared.
    route: str
    distance: float | None = None
    graph_depth: int | None = None
    matched_tags: int | None = None


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A whole retrieval pass. Counts and ids only — never content."""

    hits: list[SearchHit] = field(default_factory=list)
    seed_count: int = 0
    expanded_count: int = 0
    tags_walked: int = 0

    @property
    def total(self) -> int:
        return len(self.hits)


def search(
    query: str,
    *,
    items: ItemRepository,
    tags: TagRepository,
    embedder: Embedder,
    limit: int = 20,
    seed_limit: int = 10,
    settings: Settings | None = None,
) -> SearchResult:
    """Retrieve items for a free-text query.

    Raises ``ValueError`` on an empty query rather than embedding whitespace
    and returning whatever happens to be nearest to it.
    """
    resolved = settings or get_settings()
    cleaned = query.strip()
    if not cleaned:
        raise ValueError("query must not be empty")

    vectors = embedder.embed([cleaned])
    if not vectors:
        raise ValueError("embedder returned no vector for the query")

    seeds = items.nearest(vector=vectors[0], limit=seed_limit)
    seed_ids = [item.id for item, _distance in seeds]

    hits: list[SearchHit] = [
        SearchHit(
            item_id=item.id,
            # Cosine distance in [0, 2]; invert so larger is better and seeds
            # land above EXPANDED_SCORE_CEILING.
            score=EXPANDED_SCORE_CEILING + max(0.0, 1.0 - distance / 2.0),
            route="seed",
            distance=distance,
        )
        for item, distance in seeds
    ]

    reached, tag_depth = _walk(tags, seed_ids=seed_ids, settings=resolved)
    expanded = 0
    if reached:
        for item_id, matches, depth in _aggregate(
            tags.item_tag_pairs(list(reached), exclude=seed_ids), tag_depth
        ):
            hits.append(
                SearchHit(
                    item_id=item_id,
                    score=_expanded_score(depth=depth, matches=matches),
                    route="expanded",
                    graph_depth=depth,
                    matched_tags=matches,
                )
            )
            expanded += 1

    # The repository excludes seeds in SQL; this guards the same property in
    # case that ever changes, because a duplicated row in a result list is both
    # obvious to a user and easy to reintroduce.
    hits = _dedupe(hits)
    hits.sort(key=lambda hit: hit.score, reverse=True)
    result = SearchResult(
        hits=hits[:limit],
        seed_count=len(seeds),
        expanded_count=expanded,
        tags_walked=len(reached),
    )
    logger.info(
        "search complete",
        extra=log_context(
            chars=len(cleaned),
            seeds=result.seed_count,
            expanded=result.expanded_count,
            tags_walked=result.tags_walked,
            returned=result.total,
        ),
    )
    return result


def _dedupe(hits: list[SearchHit]) -> list[SearchHit]:
    """Keep the highest-scoring hit per item."""
    best: dict[uuid.UUID, SearchHit] = {}
    for hit in hits:
        current = best.get(hit.item_id)
        if current is None or hit.score > current.score:
            best[hit.item_id] = hit
    return list(best.values())


def _walk(
    tags: TagRepository, *, seed_ids: list[uuid.UUID], settings: Settings
) -> tuple[set[uuid.UUID], dict[uuid.UUID, int]]:
    """Collect the seeds' tags plus everything within the depth bound.

    Both directions: a broader parent and a narrower child are each plausibly
    relevant to a query that landed in between.
    """
    if not seed_ids:
        return set(), {}

    reached: set[uuid.UUID] = set()
    depths: dict[uuid.UUID, int] = {}

    for tag_id in tags.tag_ids_for_items(seed_ids):
        reached.add(tag_id)
        depths[tag_id] = 0
        # ancestors/descendants enforce the bound; nothing here walks freely.
        for ref in tags.ancestors(tag_id) + tags.descendants(tag_id):
            reached.add(ref.tag_id)
            depths[ref.tag_id] = min(depths.get(ref.tag_id, ref.depth), ref.depth)

    return reached, depths


def _aggregate(
    pairs: list[tuple[uuid.UUID, uuid.UUID]], tag_depth: dict[uuid.UUID, int]
) -> list[tuple[uuid.UUID, int, int]]:
    """Fold ``(item, tag)`` pairs into ``(item, matched_tags, nearest_depth)``.

    Depth is a property of the *tag* — how far the walk went to reach it — so
    an item's distance is the nearest reached tag it carries, not an average.
    """
    matches: dict[uuid.UUID, int] = {}
    nearest: dict[uuid.UUID, int] = {}
    for item_id, tag_id in pairs:
        matches[item_id] = matches.get(item_id, 0) + 1
        depth = tag_depth.get(tag_id, 1)
        nearest[item_id] = min(nearest.get(item_id, depth), depth)
    return [(item_id, matches[item_id], nearest[item_id]) for item_id in matches]


def _expanded_score(*, depth: int, matches: int) -> float:
    """Score a graph-reached item, strictly below any seed.

    Decays with distance and rises with how many reached tags it carries, but
    is capped so it can never overtake a direct vector match.
    """
    proximity = 1.0 / (1.0 + depth)
    density = min(matches, 5) / 5.0
    return EXPANDED_SCORE_CEILING * (0.7 * proximity + 0.3 * density)
