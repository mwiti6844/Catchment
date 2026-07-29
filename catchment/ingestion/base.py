"""Contract every source connector implements.

A connector's only job is to turn a source-specific payload into
:class:`RawRecord` values. Persistence — and therefore deduplication — is
handled centrally by :func:`ingest_records` through the repository layer.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from catchment.logging_config import get_logger, log_context
from catchment.storage.repositories import ItemRepository

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RawRecord:
    """A single artefact as seen at the source, before extraction.

    ``source_id`` must be stable and unique within a source — a WhatsApp
    message id, a tweet id, an RSS guid, an email Message-ID. It is half of
    the database-level uniqueness key.
    """

    source: str
    source_id: str
    kind: str
    url: str | None = None
    title: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    raw_ref: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def with_meta(self, **updates: Any) -> RawRecord:
        """Return a copy with additional metadata. The original is unchanged."""
        return replace(self, meta={**self.meta, **updates})


@dataclass(frozen=True, slots=True)
class IngestSummary:
    """Outcome of an ingestion batch. Counts and ids only — never content."""

    source: str
    seen: int = 0
    created: int = 0
    #: Ids of rows this batch actually inserted. Callers enqueue work for these
    #: and only these — re-ingested duplicates must not schedule a second job.
    created_item_ids: tuple[uuid.UUID, ...] = ()

    @property
    def duplicates(self) -> int:
        return self.seen - self.created


@runtime_checkable
class Connector(Protocol):
    """A source connector: webhook-driven or polled."""

    source: str

    def fetch(self) -> Iterable[RawRecord]:
        """Yield records not yet known to be ingested.

        Connectors may over-fetch; the unique constraint on
        ``(source, source_id)`` makes re-ingestion a no-op.
        """
        ...


def ingest_records(
    records: Iterable[RawRecord], repository: ItemRepository
) -> IngestSummary:
    """Persist records through the repository layer, skipping duplicates."""
    seen = 0
    source = ""
    created_ids: list[uuid.UUID] = []

    for record in records:
        seen += 1
        source = source or record.source
        item, was_created = repository.upsert(
            source=record.source,
            source_id=record.source_id,
            kind=record.kind,
            url=record.url,
            title=record.title,
            author=record.author,
            published_at=record.published_at,
            raw_ref=record.raw_ref,
            meta=record.meta,
        )
        if was_created:
            created_ids.append(item.id)

    summary = IngestSummary(
        source=source,
        seen=seen,
        created=len(created_ids),
        created_item_ids=tuple(created_ids),
    )
    logger.info(
        "ingestion batch complete",
        extra=log_context(
            source=summary.source,
            seen=summary.seen,
            created=summary.created,
            duplicates=summary.duplicates,
        ),
    )
    return summary
