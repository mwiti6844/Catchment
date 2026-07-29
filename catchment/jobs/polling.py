"""Scheduled polling for sources that have no webhook.

Same shape as the WhatsApp webhook path — ingest, then enqueue pipeline work
for newly created items — but triggered on a schedule rather than by an
inbound request.

Run from cron, or enqueue :func:`poll_email` onto the queue:

    catchment-poll-email                    # runs inline, cron-friendly
"""

from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass

from catchment.dependencies import TaskQueue
from catchment.ingestion.base import Connector, ingest_records
from catchment.ingestion.email_imap import ImapError, build_connector
from catchment.jobs.queue import get_pipeline_queue
from catchment.logging_config import configure_logging, get_logger, log_context
from catchment.storage.db import dispose_engine, session_scope
from catchment.storage.repositories import ItemRepository

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PollSummary:
    """Outcome of one poll. Counts only — never subjects or bodies."""

    source: str
    seen: int
    created: int
    queued: int

    @property
    def duplicates(self) -> int:
        return self.seen - self.created


def poll_source(connector: Connector, queue: TaskQueue) -> PollSummary:
    """Fetch from a connector, ingest, and enqueue work for what was new.

    The connector is free to over-fetch — the unique constraint on
    ``(source, source_id)`` absorbs repeats, and only genuinely new rows get a
    job.
    """
    records = list(connector.fetch())

    with session_scope() as session:
        summary = ingest_records(records, ItemRepository(session))
    # Outside the block, so the insert is committed and visible before any
    # worker can claim a job referencing it.

    created_ids: tuple[uuid.UUID, ...] = summary.created_item_ids
    for item_id in created_ids:
        # No text: the IMAP connector deliberately does not read bodies, so
        # extraction for email is a later slice.
        queue.enqueue(item_id=str(item_id), text=None)

    result = PollSummary(
        source=connector.source,
        seen=summary.seen,
        created=summary.created,
        queued=len(created_ids),
    )
    logger.info(
        "poll complete",
        extra=log_context(
            source=result.source,
            seen=result.seen,
            created=result.created,
            queued=result.queued,
            duplicates=result.duplicates,
        ),
    )
    return result


def poll_email() -> PollSummary:
    """RQ job / cron entry point: poll the configured IMAP folder."""
    return poll_source(build_connector(), get_pipeline_queue())


def main() -> int:
    """Console entry point for ``catchment-poll-email``."""
    configure_logging()
    try:
        poll_email()
    except ImapError:
        # Message already carries the folder or failure mode, never a secret.
        logger.exception("email poll failed")
        return 1
    finally:
        dispose_engine()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
