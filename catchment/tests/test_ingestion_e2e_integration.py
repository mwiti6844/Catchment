"""End-to-end: a signed WhatsApp webhook becomes an item, an extraction and a tag.

This is the slice-one definition of done — the first time content moves through
the whole system rather than through one layer of it.

Scope, stated honestly: the RQ job is executed inline by :class:`InlineQueue`
rather than round-tripped through Redis, so this covers webhook → repository →
pipeline → Postgres, but not job serialisation or worker startup. Everything
here runs inside the transaction the ``db_session`` fixture rolls back.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from catchment.api import create_app
from catchment.classification.placeholder import UNCLASSIFIED_SLUG
from catchment.dependencies import IngestionUnitOfWork, get_ingestion_unit_of_work, get_task_queue
from catchment.extraction.passthrough import PASSTHROUGH_EXTRACTOR
from catchment.ingestion.whatsapp import SIGNATURE_HEADER
from catchment.jobs.pipeline import run_pipeline
from catchment.storage.models import Extraction, Item, ItemTag, Tag
from catchment.storage.repositories import ItemRepository, TagRepository

pytestmark = pytest.mark.integration

APP_SECRET = "test-app-secret"
MESSAGE_ID = "wamid.E2E.001"
BODY = "Great piece on catchment hydrology — worth revisiting"


class InlineQueue:
    """Runs the real pipeline synchronously against the test session."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self.runs: list[str] = []

    def enqueue(self, *, item_id: str, text: str | None) -> None:
        self.runs.append(item_id)
        run_pipeline(
            items=ItemRepository(self._session),
            tags=TagRepository(self._session),
            item_id=uuid.UUID(item_id),
            text=text,
        )


@pytest.fixture
def queue(db_session: Session) -> InlineQueue:
    return InlineQueue(db_session)


@pytest.fixture
def client(db_session: Session, queue: InlineQueue) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_ingestion_unit_of_work] = lambda: IngestionUnitOfWork(
        items=ItemRepository(db_session), session=db_session
    )
    app.dependency_overrides[get_task_queue] = lambda: queue
    yield TestClient(app)
    app.dependency_overrides.clear()


def payload(*, message_id: str = MESSAGE_ID, body: str = BODY) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "entry-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [
                                {"wa_id": "254700000000", "profile": {"name": "David"}}
                            ],
                            "messages": [
                                {
                                    "from": "254700000000",
                                    "id": message_id,
                                    "timestamp": "1753800000",
                                    "type": "text",
                                    "text": {"body": body},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def post(client: TestClient, body_dict: dict[str, Any]) -> Any:
    raw = json.dumps(body_dict).encode("utf-8")
    digest = hmac.new(APP_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return client.post(
        "/webhook/whatsapp", content=raw, headers={SIGNATURE_HEADER: f"sha256={digest}"}
    )


def test_forwarded_message_lands_as_item_extraction_and_tag(
    client: TestClient, db_session: Session
) -> None:
    response = post(client, payload())
    assert response.status_code == 200
    assert response.json()["queued"] == 1

    item = db_session.execute(
        select(Item).where(Item.source == "whatsapp", Item.source_id == MESSAGE_ID)
    ).scalar_one()
    assert item.kind == "text"
    assert item.author == "David"
    assert item.published_at is not None

    extraction = db_session.execute(
        select(Extraction).where(Extraction.item_id == item.id)
    ).scalar_one()
    assert extraction.extractor == PASSTHROUGH_EXTRACTOR
    assert extraction.text == BODY

    tag_slug = db_session.execute(
        select(Tag.slug).join(ItemTag, ItemTag.tag_id == Tag.id).where(
            ItemTag.item_id == item.id
        )
    ).scalar_one()
    assert tag_slug == UNCLASSIFIED_SLUG


def test_message_body_is_not_copied_onto_the_item_row(
    client: TestClient, db_session: Session
) -> None:
    """Content belongs in extractions; items stay metadata-only."""
    post(client, payload())

    item = db_session.execute(
        select(Item).where(Item.source_id == MESSAGE_ID)
    ).scalar_one()

    assert item.title is None
    assert BODY not in json.dumps(item.meta)


def test_replayed_webhook_creates_no_duplicate_rows(
    client: TestClient, db_session: Session, queue: InlineQueue
) -> None:
    """The unique constraint absorbs Meta's retries."""
    post(client, payload())
    second = post(client, payload())

    assert second.status_code == 200
    assert second.json()["queued"] == 0
    assert len(queue.runs) == 1

    items = db_session.execute(
        select(func.count()).select_from(Item).where(Item.source_id == MESSAGE_ID)
    ).scalar_one()
    assert items == 1


def test_two_distinct_messages_share_one_unclassified_tag(
    client: TestClient, db_session: Session
) -> None:
    """get_or_create must converge, not coin a second 'unclassified'."""
    post(client, payload(message_id="wamid.E2E.A", body="first"))
    post(client, payload(message_id="wamid.E2E.B", body="second"))

    tags = db_session.execute(
        select(func.count()).select_from(Tag).where(Tag.slug == UNCLASSIFIED_SLUG)
    ).scalar_one()
    assignments = db_session.execute(
        select(func.count()).select_from(ItemTag)
    ).scalar_one()

    assert tags == 1
    assert assignments == 2


def test_unsigned_request_writes_nothing(
    client: TestClient, db_session: Session
) -> None:
    raw = json.dumps(payload()).encode("utf-8")
    response = client.post("/webhook/whatsapp", content=raw)

    assert response.status_code == 403
    assert db_session.execute(select(func.count()).select_from(Item)).scalar_one() == 0
