"""The ingestion pipeline: extraction, classification, and the fallback path."""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from catchment.classification.embeddings import EmbeddingUnavailable
from catchment.classification.placeholder import UNCLASSIFIED_SLUG, assign_unclassified
from catchment.classification.types import ClassificationResult, TagSuggestion
from catchment.extraction.passthrough import PASSTHROUGH_EXTRACTOR, passthrough
from catchment.jobs.pipeline import run_pipeline
from catchment.llm.errors import LLMUnavailable
from catchment.storage.models import EMBEDDING_DIM

BODY = "Forwarded article about Kenyan fintech — keep this out of the logs"


class FakeItems:
    def __init__(self, *, exists: bool = True) -> None:
        self._exists = exists
        self.extractions: list[dict[str, Any]] = []
        self.embeddings: list[dict[str, Any]] = []

    def get(self, item_id: uuid.UUID) -> Any:
        return SimpleNamespace(id=item_id) if self._exists else None

    def add_extraction(self, **kwargs: Any) -> Any:
        self.extractions.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4(), **kwargs)

    def set_embedding(self, **kwargs: Any) -> Any:
        self.embeddings.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    def nearest(self, **kwargs: Any) -> list[Any]:
        return []


class FakeTags:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.assignments: list[dict[str, Any]] = []
        self._tag_id = uuid.uuid4()

    def get_or_create(self, **kwargs: Any) -> tuple[Any, bool]:
        self.created.append(kwargs)
        return SimpleNamespace(id=self._tag_id), True

    def assign(self, **kwargs: Any) -> None:
        self.assignments.append(kwargs)

    def labels_for_items(self, item_ids: Any) -> list[str]:
        return []


class FakeEmbedder:
    """Returns a well-formed vector, or raises to exercise the fallback."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def embed(self, texts: Any) -> list[list[float]]:
        if self.error is not None:
            raise self.error
        return [[0.1] * EMBEDDING_DIM for _ in texts]


class FakeClassifier:
    def __init__(
        self, suggestions: list[TagSuggestion] | None = None, error: Exception | None = None
    ) -> None:
        self.error = error
        self._suggestions = suggestions or [TagSuggestion(label="Kenyan Fintech", confidence=0.9)]

    def classify(self, text: str, *, known_tags: list[str]) -> ClassificationResult:
        if self.error is not None:
            raise self.error
        return ClassificationResult(
            suggestions=self._suggestions, model="stub-1", trace_id="trace-p"
        )


@pytest.fixture
def items() -> FakeItems:
    return FakeItems()


@pytest.fixture
def tags() -> FakeTags:
    return FakeTags()


def _run(
    items: FakeItems,
    tags: FakeTags,
    text: str | None,
    *,
    embedder: Any = None,
    classifier: Any = None,
) -> Any:
    """Always inject the embedder and classifier.

    Without them the pipeline builds the real HTTP embedder and attempts a
    connection, which makes the test slow and network-dependent — and passes
    only because the failure lands in the fallback path.
    """
    return run_pipeline(
        items=items,  # type: ignore[arg-type]
        tags=tags,  # type: ignore[arg-type]
        item_id=uuid.uuid4(),
        text=text,
        embedder=embedder or FakeEmbedder(),
        classifier=classifier or FakeClassifier(),
    )


# --------------------------------------------------------------------------- #
# Passthrough extractor
# --------------------------------------------------------------------------- #


def test_passthrough_wraps_source_text() -> None:
    result = passthrough("  hello there  ")

    assert result is not None
    assert result.extractor == PASSTHROUGH_EXTRACTOR
    assert result.text == "hello there"
    assert result.confidence == 1.0


@pytest.mark.parametrize("text", [None, "", "   ", "\n\t "])
def test_passthrough_returns_none_for_nothing_to_extract(text: str | None) -> None:
    """A captionless image is normal, not an error."""
    assert passthrough(text) is None


# --------------------------------------------------------------------------- #
# Placeholder classification
# --------------------------------------------------------------------------- #


def test_placeholder_tag_is_marked_as_a_rule_not_a_model_decision(
    tags: FakeTags,
) -> None:
    """Provenance matters — this is deterministic, not an LLM guess."""
    assign_unclassified(tags=tags, item_id=uuid.uuid4())  # type: ignore[arg-type]

    assert tags.created[0]["slug"] == UNCLASSIFIED_SLUG
    assert tags.created[0]["origin"] == "import"
    assert tags.assignments[0]["assigned_by"] == "import"
    assert tags.assignments[0]["confidence"] == 1.0


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


def test_text_message_produces_an_extraction_and_a_tag(
    items: FakeItems, tags: FakeTags
) -> None:
    result = _run(items, tags, BODY)

    assert result.extracted is True
    assert result.tags_assigned == 1
    assert items.extractions[0]["extractor"] == PASSTHROUGH_EXTRACTOR
    assert items.extractions[0]["text"] == BODY
    assert len(tags.assignments) == 1


def test_item_without_text_is_still_tagged(items: FakeItems, tags: FakeTags) -> None:
    """A captionless image must still land in the review queue."""
    result = _run(items, tags, None)

    assert result.extracted is False
    assert items.extractions == []
    assert len(tags.assignments) == 1


def test_whitespace_only_text_produces_no_extraction(
    items: FakeItems, tags: FakeTags
) -> None:
    result = _run(items, tags, "   \n  ")

    assert result.extracted is False
    assert items.extractions == []


def test_missing_item_raises_rather_than_silently_succeeding(tags: FakeTags) -> None:
    with pytest.raises(LookupError, match="does not exist"):
        _run(FakeItems(exists=False), tags, BODY)


def test_pipeline_logs_no_message_content(
    items: FakeItems, tags: FakeTags, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        _run(items, tags, BODY)

    emitted = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert BODY not in emitted
    assert "item_id" in emitted


def test_successful_classification_stores_an_embedding(
    items: FakeItems, tags: FakeTags
) -> None:
    result = _run(items, tags, BODY)

    assert result.classified is True
    assert result.trace_id == "trace-p"
    assert len(items.embeddings[0]["vector"]) == EMBEDDING_DIM
    assert tags.assignments[0]["assigned_by"] == "llm"


def test_embedder_outage_falls_back_rather_than_stranding_the_item(
    items: FakeItems, tags: FakeTags
) -> None:
    """A classifier outage must not leave an item outside the review queue."""
    result = _run(
        items, tags, BODY, embedder=FakeEmbedder(error=EmbeddingUnavailable("down"))
    )

    assert result.classified is False
    assert result.tags_assigned == 1
    assert tags.created[0]["slug"] == UNCLASSIFIED_SLUG
    assert tags.assignments[0]["assigned_by"] == "import"
    # The extraction still landed — only classification degraded.
    assert items.extractions[0]["text"] == BODY


def test_llm_outage_falls_back(items: FakeItems, tags: FakeTags) -> None:
    result = _run(
        items, tags, BODY, classifier=FakeClassifier(error=LLMUnavailable("429"))
    )

    assert result.classified is False
    assert tags.created[0]["slug"] == UNCLASSIFIED_SLUG


def test_unparseable_model_response_falls_back(
    items: FakeItems, tags: FakeTags
) -> None:
    """ClassificationParseError is a ValueError; it must not kill the job."""
    result = _run(items, tags, BODY, classifier=FakeClassifier(error=ValueError("bad")))

    assert result.classified is False
    assert tags.created[0]["slug"] == UNCLASSIFIED_SLUG


def test_fallback_logs_the_failure_class_not_the_provider_message(
    items: FakeItems, tags: FakeTags, caplog: pytest.LogCaptureFixture
) -> None:
    leaky = LLMUnavailable(f"provider echoed the prompt: {BODY}")

    with caplog.at_level(logging.WARNING):
        _run(items, tags, BODY, classifier=FakeClassifier(error=leaky))

    emitted = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert BODY not in emitted
    assert "LLMUnavailable" in emitted


def test_untexted_item_gets_the_placeholder_tag(
    items: FakeItems, tags: FakeTags
) -> None:
    _run(items, tags, None)

    assert tags.created[0]["slug"] == UNCLASSIFIED_SLUG
    assert items.embeddings == [], "nothing to embed without text"
