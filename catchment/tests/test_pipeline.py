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
from catchment.config import MissingConfiguration
from catchment.extraction.passthrough import PASSTHROUGH_EXTRACTOR, passthrough
from catchment.jobs.pipeline import run_pipeline
from catchment.llm.errors import LLMUnavailable
from catchment.storage.models import EMBEDDING_DIM

BODY = "Forwarded article about Kenyan fintech — keep this out of the logs"


class FakeItems:
    def __init__(
        self,
        *,
        exists: bool = True,
        meta: dict[str, Any] | None = None,
        raw_ref: str | None = None,
    ) -> None:
        self._exists = exists
        # Mirrors the real row: the media path reads both of these, and a
        # double without them hides the branch entirely.
        self._meta = meta or {}
        self.raw_ref = raw_ref
        self.extractions: list[dict[str, Any]] = []
        self.embeddings: list[dict[str, Any]] = []

    def get(self, item_id: uuid.UUID) -> Any:
        if not self._exists:
            return None
        return SimpleNamespace(id=item_id, meta=self._meta, raw_ref=self.raw_ref)

    def set_raw_ref(self, *, item_id: uuid.UUID, raw_ref: str) -> None:
        self.raw_ref = raw_ref

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

    def existing_slugs(self, slugs: Any) -> set[str]:
        return set()


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
    store: Any = None,
    failures: Any = None,
    settings: Any = None,
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
        store=store,
        failures=failures,
        settings=settings,
    )


# --------------------------------------------------------------------------- #
# Media fetch
# --------------------------------------------------------------------------- #


class FakeStore:
    """Records what was stored, so a skipped fetch is distinguishable."""

    def __init__(self) -> None:
        self.puts: list[str] = []

    def put(self, key: str, data: bytes) -> str:
        self.puts.append(key)
        return f"blob://{key}"

    def open(self, ref: str) -> bytes:
        return b""

    def exists(self, ref: str) -> bool:
        return True

    def delete(self, ref: str) -> None:
        return None


class FakeFailures:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    def record(self, **kwargs: Any) -> Any:
        self.recorded.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())


def media_settings(**overrides: Any) -> Any:
    from catchment.config import Settings

    return Settings(
        database_url="postgresql+psycopg://u:p@localhost:5432/db",
        redis_url="redis://localhost:6379/0",
        **overrides,
    )


def test_an_item_with_no_media_never_reaches_the_fetcher(
    tags: FakeTags, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A text message must not pay for a code path it does not use."""
    monkeypatch.setattr(
        "catchment.jobs.pipeline.fetch_media",
        lambda **kwargs: pytest.fail("text items have no media to fetch"),
    )

    result = _run(FakeItems(), tags, BODY)

    assert result.media_fetched is False


def test_media_is_fetched_and_the_ref_recorded(
    tags: FakeTags, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = FakeItems(meta={"wa_media_id": "media-1"})
    monkeypatch.setattr(
        "catchment.jobs.pipeline.fetch_media",
        lambda **kwargs: SimpleNamespace(
            ref="blob://whatsapp/x.ogg", mime_type="audio/ogg", size_bytes=9
        ),
    )

    result = _run(items, tags, None, store=FakeStore(), settings=media_settings())

    assert result.media_fetched is True
    assert items.raw_ref == "blob://whatsapp/x.ogg"


def test_media_already_fetched_is_not_downloaded_again(
    tags: FakeTags, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job is retryable; a retry must not re-download what it already has."""
    items = FakeItems(meta={"wa_media_id": "media-1"}, raw_ref="blob://already/there")
    monkeypatch.setattr(
        "catchment.jobs.pipeline.fetch_media",
        lambda **kwargs: pytest.fail("already fetched"),
    )

    assert _run(items, tags, None, settings=media_settings()).media_fetched is False


def test_a_failed_fetch_leaves_the_item_alive_and_records_the_failure(
    tags: FakeTags, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unfetchable voice note still arrives as a reviewable item."""
    from catchment.ingestion.media import MediaFetchError

    items = FakeItems(meta={"wa_media_id": "media-1"})
    failures = FakeFailures()

    def boom(**kwargs: Any) -> Any:
        raise MediaFetchError("media media-1 download was empty")

    monkeypatch.setattr("catchment.jobs.pipeline.fetch_media", boom)

    result = _run(items, tags, None, failures=failures, settings=media_settings())

    assert result.media_fetched is False
    assert items.raw_ref is None, "no ref may point at a blob that was never stored"
    assert result.tags_assigned == 1, "the item is still tagged and reviewable"
    assert failures.recorded[0]["stage"] == "media_fetch"


def test_an_unconfigured_access_token_degrades_rather_than_failing(
    tags: FakeTags, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No token is a deploy problem; it must not also be a lost item.

    This runs the real fetch_media, so it exercises the actual configuration
    check rather than a stand-in for it.
    """
    items = FakeItems(meta={"wa_media_id": "media-1"})
    failures = FakeFailures()

    result = _run(items, tags, None, failures=failures, settings=media_settings())

    assert result.media_fetched is False
    assert failures.recorded[0]["error_type"] == "MissingConfiguration"


def test_the_media_id_is_logged_but_never_the_bytes(
    tags: FakeTags, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    items = FakeItems(meta={"wa_media_id": "media-1"})
    monkeypatch.setattr(
        "catchment.jobs.pipeline.fetch_media",
        lambda **kwargs: SimpleNamespace(
            ref="blob://whatsapp/x.ogg", mime_type="audio/ogg", size_bytes=9
        ),
    )

    with caplog.at_level(logging.INFO):
        _run(items, tags, BODY, store=FakeStore(), settings=media_settings())

    emitted = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert BODY not in emitted
    assert "media_fetched" in emitted


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


def test_an_unconfigured_provider_falls_back_rather_than_killing_the_job(
    items: FakeItems, tags: FakeTags
) -> None:
    """A missing API key is an outage like any other.

    MissingConfiguration is a RuntimeError, not an LLMError, so it used to
    escape this handler and fail the whole job — losing the item instead of
    degrading it. A deploy that forgot a key would silently stop tagging while
    every item still landed, which is the failure mode the fallback exists to
    prevent.
    """
    result = _run(
        items,
        tags,
        BODY,
        classifier=FakeClassifier(error=MissingConfiguration("no API key")),
    )

    assert result.classified is False
    assert tags.created[0]["slug"] == UNCLASSIFIED_SLUG
    assert items.extractions[0]["text"] == BODY, "extraction still landed"


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
