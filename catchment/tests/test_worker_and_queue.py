"""Worker entry point and the RQ queue adapter."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from catchment.jobs.pipeline import process_item
from catchment.jobs.queue import (
    JOB_TIMEOUT_SECONDS,
    QUEUE_NAME,
    RESULT_TTL_SECONDS,
    RQTaskQueue,
    get_pipeline_queue,
    job_description,
    reset_queue_cache,
)
from catchment.worker import main

SENSITIVE_TEXT = "personal message body that must not be logged"


class FakeRQQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[Any, dict[str, Any]]] = []

    def enqueue(self, func: Any, **kwargs: Any) -> None:
        self.enqueued.append((func, kwargs))


def test_adapter_enqueues_the_pipeline_job() -> None:
    rq_queue = FakeRQQueue()

    RQTaskQueue(rq_queue).enqueue(item_id="item-1", text=SENSITIVE_TEXT)  # type: ignore[arg-type]

    func, kwargs = rq_queue.enqueued[0]
    assert func is process_item
    assert kwargs["item_id"] == "item-1"
    assert kwargs["text"] == SENSITIVE_TEXT
    assert kwargs["result_ttl"] == RESULT_TTL_SECONDS
    assert kwargs["job_timeout"] == JOB_TIMEOUT_SECONDS


def test_job_description_excludes_the_payload() -> None:
    """RQ logs job.description at INFO. Its auto-generated default renders the
    call signature, which would put the message body in the worker log."""
    rq_queue = FakeRQQueue()

    RQTaskQueue(rq_queue).enqueue(item_id="item-1", text=SENSITIVE_TEXT)  # type: ignore[arg-type]

    description = rq_queue.enqueued[0][1]["description"]
    assert SENSITIVE_TEXT not in description
    assert "item-1" in description


def test_description_helper_is_content_free() -> None:
    assert job_description("abc") == (
        "catchment.jobs.pipeline.process_item(item_id='abc')"
    )


def test_enqueue_logs_the_item_not_the_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The job payload carries message text; the log line must not."""
    with caplog.at_level(logging.INFO):
        RQTaskQueue(FakeRQQueue()).enqueue(item_id="item-1", text=SENSITIVE_TEXT)  # type: ignore[arg-type]

    emitted = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert SENSITIVE_TEXT not in emitted
    assert "item-1" in emitted


def test_pipeline_queue_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("catchment.jobs.queue.build_queue", lambda: FakeRQQueue())
    reset_queue_cache()

    assert get_pipeline_queue() is get_pipeline_queue()
    reset_queue_cache()


def test_worker_starts_on_the_pipeline_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, Any] = {}

    class FakeWorker:
        def __init__(self, queues: list[str], connection: Any) -> None:
            observed["queues"] = queues
            observed["connection"] = connection

        def work(self, with_scheduler: bool) -> None:
            observed["with_scheduler"] = with_scheduler

    monkeypatch.setattr("catchment.worker.Worker", FakeWorker)
    monkeypatch.setattr(
        "catchment.worker.Redis", SimpleNamespace(from_url=lambda url: f"conn:{url}")
    )

    assert main() == 0
    assert observed["queues"] == [QUEUE_NAME]
    assert observed["with_scheduler"] is False
    assert str(observed["connection"]).startswith("conn:redis://")
