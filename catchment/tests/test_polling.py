"""Scheduled polling: ingest, then enqueue only what was genuinely new."""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from catchment.ingestion.base import RawRecord
from catchment.ingestion.email_imap import ImapError
from catchment.jobs import polling
from catchment.jobs.polling import PollSummary, poll_source

SUBJECT = "Quarterly report — must not appear in a log"


class FakeConnector:
    def __init__(self, records: list[RawRecord], source: str = "email") -> None:
        self.source = source
        self._records = records
        self.fetches = 0

    def fetch(self) -> list[RawRecord]:
        self.fetches += 1
        return self._records


class FakeQueue:
    def __init__(self, events: list[str] | None = None) -> None:
        self.jobs: list[tuple[str, str | None]] = []
        self.events = events if events is not None else []

    def enqueue(self, *, item_id: str, text: str | None) -> None:
        self.jobs.append((item_id, text))
        self.events.append("enqueue")


class FakeItemRepository:
    """Models the unique constraint, like the real one."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], Any] = {}

    def upsert(self, **kwargs: Any) -> tuple[Any, bool]:
        key = (kwargs["source"], kwargs["source_id"])
        created = key not in self.rows
        if created:
            self.rows[key] = SimpleNamespace(id=uuid.uuid4(), **kwargs)
        return self.rows[key], created


@pytest.fixture
def repo() -> FakeItemRepository:
    return FakeItemRepository()


@pytest.fixture
def events() -> list[str]:
    return []


@pytest.fixture
def wired(
    monkeypatch: pytest.MonkeyPatch, repo: FakeItemRepository, events: list[str]
) -> None:
    """Swap the transaction boundary for a fake that logs when it commits."""
    from contextlib import contextmanager

    @contextmanager
    def fake_scope() -> Any:
        yield object()
        events.append("commit")

    monkeypatch.setattr(polling, "session_scope", fake_scope)
    monkeypatch.setattr(polling, "ItemRepository", lambda _session: repo)


def record(source_id: str, source: str = "email") -> RawRecord:
    return RawRecord(
        source=source,
        source_id=source_id,
        kind="text",
        title=SUBJECT,
        meta={"folder": "INBOX"},
    )


def test_new_messages_are_ingested_and_queued(wired: None) -> None:
    queue = FakeQueue()
    connector = FakeConnector([record("<a@x>"), record("<b@x>")])

    summary = poll_source(connector, queue)

    assert summary == PollSummary(source="email", seen=2, created=2, queued=2)
    assert len(queue.jobs) == 2


def test_email_jobs_carry_no_text(wired: None) -> None:
    """The IMAP connector never reads bodies; extraction is a later slice."""
    queue = FakeQueue()

    poll_source(FakeConnector([record("<a@x>")]), queue)

    assert queue.jobs[0][1] is None


def test_refetched_messages_do_not_requeue(wired: None) -> None:
    """Over-fetching is the documented contract — repeats cost one no-op insert."""
    queue = FakeQueue()
    connector = FakeConnector([record("<a@x>"), record("<b@x>")])

    poll_source(connector, queue)
    second = poll_source(connector, queue)

    assert second.created == 0
    assert second.queued == 0
    assert second.duplicates == 2
    assert len(queue.jobs) == 2, "the second poll must not re-enqueue"


def test_commit_precedes_every_enqueue(wired: None, events: list[str]) -> None:
    """Same race as the webhook: a job must never outrun its row."""
    queue = FakeQueue(events)

    poll_source(FakeConnector([record("<a@x>"), record("<b@x>")]), queue)

    assert events == ["commit", "enqueue", "enqueue"]


def test_empty_mailbox_is_a_no_op(wired: None) -> None:
    queue = FakeQueue()

    summary = poll_source(FakeConnector([]), queue)

    assert summary.seen == 0
    assert queue.jobs == []


def test_poll_logs_counts_not_correspondence(
    wired: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        poll_source(FakeConnector([record("<a@x>")]), FakeQueue())

    emitted = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert SUBJECT not in emitted
    assert "seen" in emitted


def test_poll_email_wires_the_imap_connector(
    monkeypatch: pytest.MonkeyPatch, wired: None
) -> None:
    queue = FakeQueue()
    connector = FakeConnector([record("<a@x>")])
    monkeypatch.setattr(polling, "build_connector", lambda: connector)
    monkeypatch.setattr(polling, "get_pipeline_queue", lambda: queue)

    summary = polling.poll_email()

    assert connector.fetches == 1
    assert summary.queued == 1


def test_main_returns_one_when_the_mailbox_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode() -> None:
        raise ImapError("cannot open folder 'INBOX'")

    monkeypatch.setattr(polling, "poll_email", explode)
    monkeypatch.setattr(polling, "dispose_engine", lambda: None)

    assert polling.main() == 1
