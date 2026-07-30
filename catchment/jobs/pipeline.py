"""The ingestion pipeline.

Two entry points, deliberately split:

* :func:`run_pipeline` takes repositories and does the work — testable with
  fakes or a transactional session, no Redis and no engine involved.
* :func:`process_item` is what RQ actually calls; it owns the session and
  delegates. Keeping the transaction boundary out of the logic is what lets
  the end-to-end test run the real pipeline inside a rolled-back transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Any

from catchment.classification.embeddings import Embedder, EmbeddingError, get_embedder
from catchment.classification.llm_classifier import LLMClassifier
from catchment.classification.placeholder import assign_unclassified
from catchment.classification.service import classify_item
from catchment.classification.types import Classifier
from catchment.config import MissingConfiguration, Settings, get_settings
from catchment.extraction import ExtractionResult
from catchment.extraction.article import ArticleExtractionError, extract_article
from catchment.extraction.passthrough import passthrough
from catchment.ingestion.media import MediaFetchError, fetch_media
from catchment.llm.errors import LLMError
from catchment.logging_config import get_logger, log_context
from catchment.storage.blobs import BlobStore, FilesystemBlobStore
from catchment.storage.db import session_scope
from catchment.storage.repositories import (
    ItemRepository,
    PipelineFailureRepository,
    TagRepository,
)

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """What one pipeline run produced. Counts and ids only — no content."""

    item_id: uuid.UUID
    extracted: bool
    tags_assigned: int
    classified: bool = False
    trace_id: str | None = None
    #: True when media bytes were fetched into blob storage on this run.
    media_fetched: bool = False


def run_pipeline(
    *,
    items: ItemRepository,
    tags: TagRepository,
    item_id: uuid.UUID,
    text: str | None,
    embedder: Embedder | None = None,
    classifier: Classifier | None = None,
    failures: PipelineFailureRepository | None = None,
    store: BlobStore | None = None,
    settings: Settings | None = None,
) -> PipelineResult:
    """Fetch any media, extract source-supplied text, then classify.

    Raises ``LookupError`` if the item is gone — that means the job outlived
    its row, and retrying will not help.
    """
    resolved = settings or get_settings()

    item = items.get(item_id)
    if item is None:
        raise LookupError(f"item {item_id} does not exist")

    media_fetched = _fetch_media(
        items=items,
        item=item,
        item_id=item_id,
        failures=failures,
        store=store,
        settings=resolved,
    )

    # Article text beats a source-supplied caption: a shared link whose message
    # is "worth reading" classifies on the word "reading" without this.
    extraction = _extract_article(item=item, item_id=item_id, failures=failures)
    if extraction is None:
        extraction = passthrough(text)
    if extraction is not None:
        items.add_extraction(
            item_id=item_id,
            extractor=extraction.extractor,
            text=extraction.text,
            language=extraction.language,
            confidence=extraction.confidence,
            meta=extraction.meta,
        )

    result = _classify(
        items=items,
        tags=tags,
        item_id=item_id,
        text=extraction.text if extraction is not None else None,
        embedder=embedder,
        classifier=classifier,
        failures=failures,
        settings=resolved,
    )

    logger.info(
        "pipeline complete",
        extra=log_context(
            item_id=str(item_id),
            extracted=extraction is not None,
            classified=result.classified,
            tags_assigned=result.tags_assigned,
            media_fetched=media_fetched,
        ),
    )
    return replace(result, media_fetched=media_fetched)


def _fetch_media(
    *,
    items: ItemRepository,
    item: Any,
    item_id: uuid.UUID,
    failures: PipelineFailureRepository | None,
    store: BlobStore | None,
    settings: Settings,
) -> bool:
    """Download this item's media into blob storage, if it has any and has not.

    Degrades rather than raising, for the same reason classification does: an
    unfetchable voice note should still arrive as a reviewable item, and the
    failure belongs in the dead-letter table where a human can see it — not in
    RQ's failed registry, which the dashboard cannot read.

    Idempotent by the ``raw_ref`` check, so a retried job does not re-download
    media it already has.
    """
    media_id = (item.meta or {}).get("wa_media_id")
    if not isinstance(media_id, str) or item.raw_ref is not None:
        return False

    try:
        fetched = fetch_media(
            media_id=media_id,
            item_id=item_id,
            store=store or FilesystemBlobStore(settings.blob_root),
            settings=settings,
        )
    except (MediaFetchError, MissingConfiguration) as error:
        # MediaNotAvailable is a subclass: expired media is permanent, but it
        # degrades the same way. The distinction matters to a retry policy,
        # not to whether this item survives.
        logger.warning(
            "media fetch failed; item continues without it",
            extra=log_context(
                item_id=str(item_id),
                media_id=media_id,
                error=type(error).__name__,
            ),
        )
        if failures is not None:
            failures.record(
                item_id=item_id,
                stage="media_fetch",
                error_type=type(error).__name__,
            )
        return False

    items.set_raw_ref(item_id=item_id, raw_ref=fetched.ref)
    return True


def _extract_article(
    *,
    item: Any,
    item_id: uuid.UUID,
    failures: PipelineFailureRepository | None,
) -> ExtractionResult | None:
    """Fetch and parse this item's URL, if it has one worth fetching.

    Returns None when there is nothing to do, so the caller falls back to
    whatever text the source supplied. A paywalled or dead link is an ordinary
    outcome — the item keeps its caption and stays reviewable.
    """
    if item.kind not in ("link", "article") or not item.url:
        return None

    try:
        return extract_article(item.url)
    except ArticleExtractionError as error:
        logger.info(
            "article extraction failed; falling back to source text",
            extra=log_context(item_id=str(item_id), error=type(error).__name__),
        )
        if failures is not None:
            failures.record(
                item_id=item_id,
                stage="article_extraction",
                error_type=type(error).__name__,
            )
        return None


def _classify(
    *,
    items: ItemRepository,
    tags: TagRepository,
    item_id: uuid.UUID,
    text: str | None,
    embedder: Embedder | None,
    classifier: Classifier | None,
    failures: PipelineFailureRepository | None,
    settings: Settings,
) -> PipelineResult:
    """Classify an item, degrading to the placeholder rather than losing it.

    An item with no text cannot be classified at all (a captionless image until
    OCR lands), and a classifier outage should not strand an item outside the
    review queue. Both paths fall back to ``unclassified``, which is a visible
    "awaiting classification" marker rather than a silent drop.
    """
    if not text:
        assign_unclassified(tags=tags, item_id=item_id)
        return PipelineResult(
            item_id=item_id, extracted=False, tags_assigned=1, classified=False
        )

    try:
        outcome = classify_item(
            items=items,
            tags=tags,
            embedder=embedder or get_embedder(settings),
            classifier=classifier or LLMClassifier(),
            item_id=item_id,
            text=text,
            settings=settings,
        )
    except (EmbeddingError, LLMError, MissingConfiguration, ValueError) as error:
        # ValueError covers ClassificationParseError. MissingConfiguration is a
        # plain RuntimeError and used to escape here, killing the job: a deploy
        # that forgot an API key lost items instead of degrading them. It is an
        # outage like any other, and the failure row below is what makes it
        # visible. Log the failure class, never the provider's message — it can
        # quote the submitted text.
        logger.warning(
            "classification failed; falling back to unclassified",
            extra=log_context(item_id=str(item_id), error=type(error).__name__),
        )
        if failures is not None:
            # Makes the degradation visible in the review queue. Without it,
            # "classifier was down" is indistinguishable from "nothing to tag".
            failures.record(
                item_id=item_id,
                stage="classification",
                error_type=type(error).__name__,
            )
        assign_unclassified(tags=tags, item_id=item_id)
        return PipelineResult(
            item_id=item_id, extracted=True, tags_assigned=1, classified=False
        )

    return PipelineResult(
        item_id=item_id,
        extracted=True,
        tags_assigned=outcome.assigned,
        classified=True,
        trace_id=outcome.trace_id,
    )


def process_item(item_id: str, text: str | None = None) -> PipelineResult:
    """RQ job entry point. Owns the transaction; commits or rolls back whole.

    ``item_id`` is a string because RQ serialises job arguments — keeping the
    signature to plain JSON-safe types avoids depending on the pickle format.
    """
    identifier = uuid.UUID(item_id)
    with session_scope() as session:
        return run_pipeline(
            items=ItemRepository(session),
            tags=TagRepository(session),
            failures=PipelineFailureRepository(session),
            item_id=identifier,
            text=text,
        )
