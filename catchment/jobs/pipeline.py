"""The slice-one ingestion pipeline.

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

from catchment.classification.placeholder import assign_unclassified
from catchment.extraction.passthrough import passthrough
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


def run_pipeline(
    *,
    items: ItemRepository,
    tags: TagRepository,
    item_id: uuid.UUID,
    text: str | None,
) -> PipelineResult:
    """Extract source-supplied text and apply the placeholder tag.

    Raises ``LookupError`` if the item is gone — that means the job outlived
    its row, and retrying will not help.
    """
    if items.get(item_id) is None:
        raise LookupError(f"item {item_id} does not exist")

    result = passthrough(text)
    if result is not None:
        items.add_extraction(
            item_id=item_id,
            extractor=result.extractor,
            text=result.text,
            language=result.language,
            confidence=result.confidence,
            meta=result.meta,
        )

    assign_unclassified(tags=tags, item_id=item_id)

    logger.info(
        "pipeline complete",
        extra=log_context(item_id=str(item_id), extracted=result is not None),
    )
    return PipelineResult(
        item_id=item_id, extracted=result is not None, tags_assigned=1
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
