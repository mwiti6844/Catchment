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
from dataclasses import dataclass

from catchment.classification.embeddings import Embedder, EmbeddingError, get_embedder
from catchment.classification.llm_classifier import LLMClassifier
from catchment.classification.placeholder import assign_unclassified
from catchment.classification.service import classify_item
from catchment.classification.types import Classifier
from catchment.config import Settings, get_settings
from catchment.extraction.passthrough import passthrough
from catchment.llm.errors import LLMError
from catchment.logging_config import get_logger, log_context
from catchment.storage.db import session_scope
from catchment.storage.repositories import ItemRepository, TagRepository

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """What one pipeline run produced. Counts and ids only — no content."""

    item_id: uuid.UUID
    extracted: bool
    tags_assigned: int
    classified: bool = False
    trace_id: str | None = None


def run_pipeline(
    *,
    items: ItemRepository,
    tags: TagRepository,
    item_id: uuid.UUID,
    text: str | None,
    embedder: Embedder | None = None,
    classifier: Classifier | None = None,
    settings: Settings | None = None,
) -> PipelineResult:
    """Extract source-supplied text, then classify.

    Raises ``LookupError`` if the item is gone — that means the job outlived
    its row, and retrying will not help.
    """
    resolved = settings or get_settings()

    if items.get(item_id) is None:
        raise LookupError(f"item {item_id} does not exist")

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
        settings=resolved,
    )

    logger.info(
        "pipeline complete",
        extra=log_context(
            item_id=str(item_id),
            extracted=extraction is not None,
            classified=result.classified,
            tags_assigned=result.tags_assigned,
        ),
    )
    return result


def _classify(
    *,
    items: ItemRepository,
    tags: TagRepository,
    item_id: uuid.UUID,
    text: str | None,
    embedder: Embedder | None,
    classifier: Classifier | None,
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
    except (EmbeddingError, LLMError, ValueError) as error:
        # ValueError covers ClassificationParseError. Log the failure class,
        # never the provider's message — it can quote the submitted text.
        logger.warning(
            "classification failed; falling back to unclassified",
            extra=log_context(item_id=str(item_id), error=type(error).__name__),
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
            item_id=identifier,
            text=text,
        )
