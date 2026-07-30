"""Vector-seeded, graph-expanded retrieval.

The property that matters most: a direct semantic match must never be displaced
by something reached two hops through the tag graph.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from catchment.config import Settings
from catchment.retrieval.graphrag import EXPANDED_SCORE_CEILING, search
from catchment.storage.models import EMBEDDING_DIM

QUERY = "catchment hydrology and drainage basins"


class FakeEmbedder:
    def __init__(self, *, empty: bool = False) -> None:
        self.empty = empty
        self.calls: list[str] = []

    def embed(self, texts: Any) -> list[list[float]]:
        self.calls.extend(texts)
        return [] if self.empty else [[0.1] * EMBEDDING_DIM for _ in texts]


class FakeItems:
    def __init__(self, seeds: list[tuple[uuid.UUID, float]] | None = None) -> None:
        self._seeds = seeds or []

    def nearest(self, **kwargs: Any) -> list[Any]:
        return [(SimpleNamespace(id=i), d) for i, d in self._seeds]


class FakeTags:
    """Graph: seed tag -> one child at depth 1, one grandchild at depth 2."""

    def __init__(
        self,
        seed_tags: list[uuid.UUID] | None = None,
        related: dict[uuid.UUID, int] | None = None,
        pairs: list[tuple[uuid.UUID, uuid.UUID]] | None = None,
    ) -> None:
        self._seed_tags = seed_tags or []
        self._related = related or {}
        self._pairs = pairs or []
        self.walk_calls = 0

    def tag_ids_for_items(self, item_ids: Any) -> list[uuid.UUID]:
        return self._seed_tags

    def ancestors(self, tag_id: uuid.UUID, **kwargs: Any) -> list[Any]:
        self.walk_calls += 1
        return []

    def descendants(self, tag_id: uuid.UUID, **kwargs: Any) -> list[Any]:
        self.walk_calls += 1
        return [
            SimpleNamespace(tag_id=t, depth=d) for t, d in self._related.items()
        ]

    def item_tag_pairs(self, tag_ids: Any, **kwargs: Any) -> list[Any]:
        # Mirrors the real repository, which excludes in SQL. A double that
        # ignored `exclude` would hide a duplicate-results bug.
        wanted = set(tag_ids)
        excluded = set(kwargs.get("exclude") or ())
        return [
            (i, t) for i, t in self._pairs if t in wanted and i not in excluded
        ]


def run(**kwargs: Any) -> Any:
    return search(
        kwargs.pop("query", QUERY),
        items=kwargs.pop("items", FakeItems()),
        tags=kwargs.pop("tags", FakeTags()),
        embedder=kwargs.pop("embedder", FakeEmbedder()),
        settings=Settings(),
        **kwargs,
    )


def test_empty_query_is_rejected() -> None:
    """Embedding whitespace would return whatever happens to be nearest to it."""
    for blank in ("", "   ", "\n\t"):
        with pytest.raises(ValueError, match="must not be empty"):
            run(query=blank)


def test_seeds_come_from_vector_search() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    result = run(items=FakeItems([(a, 0.1), (b, 0.4)]))

    assert result.seed_count == 2
    assert [h.route for h in result.hits] == ["seed", "seed"]
    assert result.hits[0].item_id == a, "nearer item ranks first"


def test_closer_vector_match_scores_higher() -> None:
    near, far = uuid.uuid4(), uuid.uuid4()
    hits = run(items=FakeItems([(far, 1.2), (near, 0.05)])).hits

    assert hits[0].item_id == near
    assert hits[0].score > hits[1].score


def test_graph_expansion_adds_items_the_vector_missed() -> None:
    """The whole point: topically related, lexically unlike the query."""
    seed, expanded = uuid.uuid4(), uuid.uuid4()
    seed_tag, child_tag = uuid.uuid4(), uuid.uuid4()

    result = run(
        items=FakeItems([(seed, 0.2)]),
        tags=FakeTags(
            seed_tags=[seed_tag],
            related={child_tag: 1},
            pairs=[(expanded, child_tag)],
        ),
    )

    assert result.expanded_count == 1
    routes = {h.item_id: h.route for h in result.hits}
    assert routes[expanded] == "expanded"
    assert routes[seed] == "seed"


def test_a_seed_always_outranks_an_expanded_hit() -> None:
    """A two-hop association must not displace a direct match, ever."""
    seed, expanded = uuid.uuid4(), uuid.uuid4()
    seed_tag, child = uuid.uuid4(), uuid.uuid4()

    # Worst case for the seed: a distant vector match, against an expanded item
    # sitting one hop away and carrying many reached tags.
    hits = run(
        items=FakeItems([(seed, 1.9)]),
        tags=FakeTags(
            seed_tags=[seed_tag],
            related={child: 1},
            pairs=[(expanded, child)] * 5,
        ),
    ).hits

    by_route = {h.route: h.score for h in hits}
    assert by_route["seed"] >= EXPANDED_SCORE_CEILING
    assert by_route["expanded"] < EXPANDED_SCORE_CEILING
    assert hits[0].route == "seed"


def test_nearer_graph_hops_outrank_further_ones() -> None:
    close_item, far_item = uuid.uuid4(), uuid.uuid4()
    seed_tag, close_tag, far_tag = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    hits = run(
        items=FakeItems([(uuid.uuid4(), 0.2)]),
        tags=FakeTags(
            seed_tags=[seed_tag],
            related={close_tag: 1, far_tag: 4},
            pairs=[(close_item, close_tag), (far_item, far_tag)],
        ),
    ).hits

    scores = {h.item_id: h.score for h in hits}
    assert scores[close_item] > scores[far_item]


def test_item_depth_is_its_nearest_reached_tag() -> None:
    """Depth belongs to the tag; an item's distance is its closest one."""
    item = uuid.uuid4()
    seed_tag, near_tag, far_tag = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    hits = run(
        items=FakeItems([(uuid.uuid4(), 0.2)]),
        tags=FakeTags(
            seed_tags=[seed_tag],
            related={near_tag: 1, far_tag: 5},
            pairs=[(item, far_tag), (item, near_tag)],
        ),
    ).hits

    expanded = next(h for h in hits if h.item_id == item)
    assert expanded.graph_depth == 1
    assert expanded.matched_tags == 2


def test_seeds_are_not_repeated_as_expanded_hits() -> None:
    seed = uuid.uuid4()
    seed_tag = uuid.uuid4()

    result = run(
        items=FakeItems([(seed, 0.2)]),
        tags=FakeTags(seed_tags=[seed_tag], related={}, pairs=[(seed, seed_tag)]),
    )

    assert [h.item_id for h in result.hits].count(seed) == 1


def test_no_seeds_means_no_walk() -> None:
    tags = FakeTags(seed_tags=[uuid.uuid4()])

    result = run(items=FakeItems([]), tags=tags)

    assert result.total == 0
    assert tags.walk_calls == 0, "nothing to expand from"


def test_limit_is_respected() -> None:
    seeds = [(uuid.uuid4(), 0.1 * n) for n in range(10)]
    assert run(items=FakeItems(seeds), limit=3).total == 3


def test_embedder_returning_nothing_raises() -> None:
    with pytest.raises(ValueError, match="no vector"):
        run(embedder=FakeEmbedder(empty=True))


def test_search_logs_no_query_text(caplog: pytest.LogCaptureFixture) -> None:
    """A query is user input and may quote correspondence back."""
    with caplog.at_level(logging.INFO):
        run(items=FakeItems([(uuid.uuid4(), 0.2)]))

    emitted = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert QUERY not in emitted
    assert "seeds" in emitted
