"""Placing a coined tag in the graph.

Assignment alone produces a flat vocabulary: every tag a peer of every other,
and a graph walk that reaches nothing. Placement is what makes the taxonomy a
graph rather than a list, so it is also the path an injected message would most
like to reach — hence the two hard rules exercised here: a parent must be a tag
the model was actually shown, and an edge that would close a cycle is refused.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from catchment.classification.prompt import SYSTEM_PROMPT, parse_response
from catchment.classification.service import classify_item
from catchment.classification.types import ClassificationResult, TagSuggestion

FIXTURE = Path(__file__).parent / "fixtures" / "classification" / "tag_edges.json"


@pytest.fixture
def fixture() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(FIXTURE.read_text())
    return payload


# --------------------------------------------------------------------------- #
# Parsing: which placements survive validation
# --------------------------------------------------------------------------- #


def test_fixture_placements_parse_as_expected(fixture: dict[str, Any]) -> None:
    """The whole decision path, driven from the fixture (CLAUDE.md)."""
    suggestions = parse_response(
        json.dumps(fixture["response"]), known_tags=fixture["known_tags"]
    )
    by_slug = {s.slug: s for s in suggestions}

    for case in fixture["expected"]:
        assert by_slug[case["slug"]].broader_than == case["broader_slug"], case["why"]


def test_parent_must_be_a_tag_the_model_was_shown() -> None:
    """An unverifiable parent is the injection route: content that names a tag
    nobody offered could otherwise attach anything anywhere in the graph."""
    raw = json.dumps(
        {"tags": [{"label": "Mobile Money", "confidence": 0.9, "broader_than": "Fintech"}]}
    )

    assert parse_response(raw, known_tags=["Machine Learning"])[0].broader_than is None
    assert parse_response(raw, known_tags=["Fintech"])[0].broader_than == "fintech"


def test_a_tag_is_never_broader_than_itself() -> None:
    raw = json.dumps(
        {"tags": [{"label": "Fintech", "confidence": 0.9, "broader_than": "fintech"}]}
    )

    assert parse_response(raw, known_tags=["Fintech"])[0].broader_than is None


def test_unusable_placement_does_not_discard_the_tag() -> None:
    """A bad parent costs the edge, not the classification."""
    raw = json.dumps(
        {"tags": [{"label": "Mobile Money", "confidence": 0.9, "broader_than": 42}]}
    )

    suggestions = parse_response(raw, known_tags=["Fintech"])

    assert len(suggestions) == 1
    assert suggestions[0].broader_than is None


def test_prompt_documents_the_placement_field() -> None:
    """A field the model is never told about is a field it never fills."""
    assert "broader_than" in SYSTEM_PROMPT


# --------------------------------------------------------------------------- #
# Applying placements
# --------------------------------------------------------------------------- #


class FakeItems:
    def __init__(self) -> None:
        self.embeddings: list[uuid.UUID] = []

    def get(self, item_id: uuid.UUID) -> Any:
        return object()

    def set_embedding(self, *, item_id: uuid.UUID, **kwargs: Any) -> None:
        self.embeddings.append(item_id)

    def nearest(self, **kwargs: Any) -> list[Any]:
        return []


class FakeTag:
    def __init__(self, slug: str) -> None:
        self.id = uuid.uuid4()
        self.slug = slug


class FakeTags:
    """Mirrors TagRepository closely enough that a missing call is visible."""

    def __init__(self, *, existing: list[str] | None = None, cyclic: bool = False) -> None:
        self.tags: dict[str, FakeTag] = {slug: FakeTag(slug) for slug in existing or []}
        self.edges: list[tuple[str, str]] = []
        self.assigned: list[uuid.UUID] = []
        self.cyclic = cyclic

    def labels_for_items(self, item_ids: Any) -> list[str]:
        return []

    def existing_slugs(self, slugs: Any) -> set[str]:
        return {s for s in slugs if s in self.tags}

    def get_or_create(self, *, slug: str, **kwargs: Any) -> tuple[FakeTag, bool]:
        if slug in self.tags:
            return self.tags[slug], False
        self.tags[slug] = FakeTag(slug)
        return self.tags[slug], True

    def get_by_slug(self, slug: str) -> FakeTag | None:
        return self.tags.get(slug)

    def assign(self, *, tag_id: uuid.UUID, **kwargs: Any) -> None:
        self.assigned.append(tag_id)

    def link_broader(self, *, parent_id: uuid.UUID, child_id: uuid.UUID) -> bool:
        if self.cyclic:
            return False
        slugs = {tag.id: tag.slug for tag in self.tags.values()}
        self.edges.append((slugs[parent_id], slugs[child_id]))
        return True


class FakeEmbedder:
    def embed(self, texts: Any) -> list[list[float]]:
        return [[0.1] * 8 for _ in texts]


class FakeClassifier:
    def __init__(self, suggestions: list[TagSuggestion]) -> None:
        self.suggestions = suggestions

    def classify(self, text: str, *, known_tags: list[str]) -> ClassificationResult:
        return ClassificationResult(suggestions=self.suggestions, model="fake")


def run(suggestions: list[TagSuggestion], tags: FakeTags) -> Any:
    return classify_item(
        items=FakeItems(),  # type: ignore[arg-type]
        tags=tags,  # type: ignore[arg-type]
        embedder=FakeEmbedder(),
        classifier=FakeClassifier(suggestions),
        item_id=uuid.uuid4(),
        text="some text",
    )


def test_a_placement_becomes_an_edge() -> None:
    tags = FakeTags(existing=["machine-learning"])

    outcome = run(
        [TagSuggestion("Retrieval Augmented Generation", 0.9, broader_than="machine-learning")],
        tags,
    )

    assert tags.edges == [("machine-learning", "retrieval-augmented-generation")]
    assert outcome.linked == 1


def test_a_parent_missing_from_the_database_is_skipped() -> None:
    """The parent was a candidate label, but the row is gone by apply time."""
    tags = FakeTags(existing=[])

    outcome = run([TagSuggestion("Mobile Money", 0.9, broader_than="fintech")], tags)

    assert tags.edges == []
    assert outcome.linked == 0


def test_a_refused_edge_still_leaves_the_tag_assigned() -> None:
    """Losing an edge must never cost the assignment that earned it."""
    tags = FakeTags(existing=["machine-learning"], cyclic=True)

    outcome = run([TagSuggestion("RAG", 0.9, broader_than="machine-learning")], tags)

    assert tags.edges == []
    assert outcome.linked == 0
    assert outcome.assigned == 1
    assert len(tags.assigned) == 1


def test_edges_per_item_are_capped(settings: Any) -> None:
    """Same reasoning as the coinage cap: bound what one injected item achieves.

    Reached with *existing* tags on purpose. New tags hit the coinage cap first
    (2 by default), so the edge cap only governs an item that re-parents
    vocabulary already in the graph — which is the more damaging case, since it
    changes what retrieval reaches for every item carrying those tags.
    """
    existing = [f"topic-{n}" for n in range(9)]
    tags = FakeTags(existing=["machine-learning", *existing])
    suggestions = [
        TagSuggestion(f"Topic {n}", 0.9, broader_than="machine-learning") for n in range(9)
    ]

    outcome = run(suggestions, tags)

    assert outcome.coined == 0, "these tags already exist; the coinage cap must not apply"
    assert outcome.assigned == 9
    assert outcome.linked == settings.max_new_edges_per_item
    assert len(tags.edges) == settings.max_new_edges_per_item
