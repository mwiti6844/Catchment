"""RQ queue wiring.

``RQTaskQueue`` adapts ``rq.Queue`` to the narrow
:class:`~catchment.dependencies.TaskQueue` protocol so request handlers never
hold a Redis connection directly.
"""

from __future__ import annotations

from typing import Final

from redis import Redis
from rq import Queue

from catchment.config import Settings, get_settings
from catchment.jobs.pipeline import process_item
from catchment.logging_config import get_logger, log_context

logger = get_logger(__name__)

QUEUE_NAME: Final[str] = "catchment"

#: Ingested content is personal, so finished jobs should not linger in Redis
#: with their arguments attached.
RESULT_TTL_SECONDS: Final[int] = 3600
JOB_TIMEOUT_SECONDS: Final[int] = 600


def job_description(item_id: str) -> str:
    """Build the label RQ prints for a job.

    Without this, RQ derives its own description by rendering the call
    signature — which puts the message body straight into ``rq.worker``'s INFO
    logs, violating the no-content-in-logs constraint from a library we do not
    control. Neither ``RedactionFilter`` (it only sees our ``extra=`` context)
    nor the AST lint (it only reads our source) can catch that, so the payload
    has to be kept out of the description in the first place.
    """
    return f"catchment.jobs.pipeline.process_item(item_id={item_id!r})"


class RQTaskQueue:
    """Enqueues pipeline work onto Redis."""

    def __init__(self, queue: Queue) -> None:
        self._queue = queue

    def enqueue(self, *, item_id: str, text: str | None) -> None:
        self._queue.enqueue(
            process_item,
            item_id=item_id,
            text=text,
            result_ttl=RESULT_TTL_SECONDS,
            job_timeout=JOB_TIMEOUT_SECONDS,
            description=job_description(item_id),
        )
        # The payload carries message text; log the item id only.
        logger.info("job enqueued", extra=log_context(item_id=item_id, queue=QUEUE_NAME))

    def close(self) -> None:
        """Release the underlying Redis connection."""
        connection = getattr(self._queue, "connection", None)
        if connection is not None:
            connection.close()


def build_queue(settings: Settings | None = None) -> Queue:
    """Construct an RQ queue from configuration."""
    resolved = settings or get_settings()
    return Queue(QUEUE_NAME, connection=Redis.from_url(str(resolved.redis_url)))


# Explicit singleton, for the same reason as the engine in ``storage/db.py``:
# shutdown needs a handle on the instance to close its connection.
_queue: RQTaskQueue | None = None


def get_pipeline_queue() -> RQTaskQueue:
    """Return the process-wide queue adapter, creating it on first use."""
    global _queue

    if _queue is None:
        _queue = RQTaskQueue(build_queue())
    return _queue


def close_pipeline_queue() -> None:
    """Close the queue's Redis connection and drop it. Called on shutdown."""
    global _queue

    if _queue is not None:
        _queue.close()
        _queue = None


def reset_queue_cache() -> None:
    """Drop the cached queue. Intended for tests only."""
    close_pipeline_queue()
