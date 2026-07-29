"""Ingestion is idempotent per ``(source, source_id)`` and never logs content."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from catchment.ingestion import RawRecord, ingest_records


class FakeItemRepository:
    """Stands in for the real repository, modelling the unique constraint."""

    def __init__(self) -> None:
        self.keys: set[tuple[str, str]] = set()
        self.calls: list[dict[str, Any]] = []

    def upsert(self, **kwargs: Any) -> tuple[object, bool]:
        self.calls.append(kwargs)
        key = (kwargs["source"], kwargs["source_id"])
        created = key not in self.keys
        self.keys.add(key)
        return object(), created


def _record(source_id: str, **overrides: Any) -> RawRecord:
    defaults: dict[str, Any] = {
        "source": "whatsapp",
        "source_id": source_id,
        "kind": "text",
    }
    return RawRecord(**{**defaults, **overrides})


@pytest.fixture
def repo() -> FakeItemRepository:
    return FakeItemRepository()


def test_new_records_are_created(repo: FakeItemRepository) -> None:
    summary = ingest_records([_record("m1"), _record("m2")], repo)  # type: ignore[arg-type]

    assert summary.seen == 2
    assert summary.created == 2
    assert summary.duplicates == 0


def test_repeated_source_id_is_a_duplicate(repo: FakeItemRepository) -> None:
    summary = ingest_records([_record("m1"), _record("m1")], repo)  # type: ignore[arg-type]

    assert summary.seen == 2
    assert summary.created == 1
    assert summary.duplicates == 1


def test_same_source_id_across_sources_is_distinct(repo: FakeItemRepository) -> None:
    records = [_record("shared-id"), _record("shared-id", source="email")]

    summary = ingest_records(records, repo)  # type: ignore[arg-type]

    assert summary.created == 2


def test_empty_batch_is_handled(repo: FakeItemRepository) -> None:
    summary = ingest_records([], repo)  # type: ignore[arg-type]
    assert summary.seen == 0
    assert summary.created == 0


def test_with_meta_returns_a_copy() -> None:
    original = _record("m1", meta={"chat": "family"})

    updated = original.with_meta(forwarded=True)

    assert original.meta == {"chat": "family"}
    assert updated.meta == {"chat": "family", "forwarded": True}
    assert updated is not original


def test_ingestion_logs_carry_counts_not_content(
    repo: FakeItemRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """The call site itself must pass counts, not the message it just ingested."""
    body = "Dinner at 8, do not put this in a log"
    record = _record("m1").with_meta(body=body)

    with caplog.at_level(logging.INFO):
        ingest_records([record], repo)  # type: ignore[arg-type]

    emitted = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert body not in emitted
    assert "seen" in emitted


def test_ingestion_log_context_survives_reserved_names(
    repo: FakeItemRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """``created`` collides with a LogRecord attribute; it must not raise."""
    with caplog.at_level(logging.INFO):
        ingest_records([_record("m1")], repo)  # type: ignore[arg-type]

    assert caplog.records[-1].ctx_created == 1  # type: ignore[attr-defined]
