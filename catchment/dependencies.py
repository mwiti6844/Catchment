"""FastAPI dependency providers.

Kept in one small module so request handlers depend on narrow interfaces —
a repository and a queue protocol — rather than on a database engine and a
Redis connection. That is what lets the webhook be tested end-to-end with an
inline queue and a transactional session.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from catchment.storage.db import session_scope
from catchment.storage.repositories import ItemRepository, TagRepository


class TaskQueue(Protocol):
    """The only queue surface the ingestion path needs.

    Narrower than ``rq.Queue`` on purpose: a fake that runs the job inline is
    a three-line class, so the webhook can be tested without Redis.
    """

    def enqueue(self, *, item_id: str, text: str | None) -> None:
        """Schedule pipeline work for a newly ingested item."""
        ...


def get_item_repository() -> Iterator[ItemRepository]:
    """Yield an item repository bound to a request-scoped transaction."""
    with session_scope() as session:
        yield ItemRepository(session)


def get_tag_repository() -> Iterator[TagRepository]:
    """Yield a tag repository bound to a request-scoped transaction."""
    with session_scope() as session:
        yield TagRepository(session)


def get_task_queue() -> TaskQueue:
    """Return the process-wide RQ-backed queue."""
    from catchment.jobs.queue import get_pipeline_queue

    return get_pipeline_queue()
