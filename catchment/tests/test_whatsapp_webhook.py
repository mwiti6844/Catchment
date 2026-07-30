"""WhatsApp webhook: signature verification, payload parsing, endpoint behaviour."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from catchment.api import create_app
from catchment.dependencies import get_ingestion_unit_of_work, get_task_queue
from catchment.ingestion.whatsapp import SIGNATURE_HEADER, parse_webhook, verify_signature

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
BODY_TEXT = "Dinner at 8 — this must never reach a log"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def envelope(
    *messages: dict[str, Any], contacts: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550001", "phone_number_id": "999"},
        "messages": list(messages),
    }
    if contacts is not None:
        value["contacts"] = contacts
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "entry-1", "changes": [{"field": "messages", "value": value}]}],
    }


def text_message(
    *, message_id: str = "wamid.TEXT1", body: str = BODY_TEXT, sender: str = "254700000000"
) -> dict[str, Any]:
    return {
        "from": sender,
        "id": message_id,
        "timestamp": "1753800000",
        "type": "text",
        "text": {"body": body},
    }


def media_message(
    *,
    kind: str = "image",
    message_id: str = "wamid.MEDIA1",
    caption: str | None = None,
    media_id: str = "media-abc",
) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": media_id, "mime_type": f"{kind}/jpeg"}
    if caption is not None:
        payload["caption"] = caption
    return {
        "from": "254700000000",
        "id": message_id,
        "timestamp": "1753800000",
        "type": kind,
        kind: payload,
    }


class FakeItemRepository:
    """Models the unique constraint on ``(source, source_id)``."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], Any] = {}
        self.calls: list[dict[str, Any]] = []

    def upsert(self, **kwargs: Any) -> tuple[Any, bool]:
        self.calls.append(kwargs)
        key = (kwargs["source"], kwargs["source_id"])
        created = key not in self.rows
        if created:
            self.rows[key] = SimpleNamespace(id=uuid.uuid4(), **kwargs)
        return self.rows[key], created


class FakeQueue:
    def __init__(self, events: list[str] | None = None) -> None:
        self.jobs: list[tuple[str, str | None]] = []
        self.events = events if events is not None else []

    def enqueue(self, *, item_id: str, text: str | None) -> None:
        self.jobs.append((item_id, text))
        self.events.append("enqueue")


class FakeUnitOfWork:
    """Stands in for the real unit of work, recording when it commits."""

    def __init__(self, items: FakeItemRepository, events: list[str]) -> None:
        self.items = items
        self.events = events
        self.session = object()

    def commit(self) -> None:
        self.events.append("commit")


@pytest.fixture
def events() -> list[str]:
    """Shared ordering log so tests can assert commit precedes enqueue."""
    return []


@pytest.fixture
def repo() -> FakeItemRepository:
    return FakeItemRepository()


@pytest.fixture
def queue(events: list[str]) -> FakeQueue:
    return FakeQueue(events)


class FakeHealth:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(self, **kwargs: Any) -> Any:
        self.records.append(kwargs)
        return SimpleNamespace(source=kwargs["source"])


@pytest.fixture
def health() -> FakeHealth:
    return FakeHealth()


@pytest.fixture
def client(
    repo: FakeItemRepository,
    queue: FakeQueue,
    events: list[str],
    health: FakeHealth,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.setattr(
        "catchment.ingestion.whatsapp.ConnectorHealthRepository",
        lambda _session: health,
    )
    app = create_app()
    app.dependency_overrides[get_ingestion_unit_of_work] = lambda: FakeUnitOfWork(
        repo, events
    )
    app.dependency_overrides[get_task_queue] = lambda: queue
    yield TestClient(app)
    app.dependency_overrides.clear()


def post(client: TestClient, payload: dict[str, Any], *, secret: str = APP_SECRET) -> Any:
    body = json.dumps(payload).encode("utf-8")
    return client.post(
        "/webhook/whatsapp", content=body, headers={SIGNATURE_HEADER: sign(body, secret)}
    )


# --------------------------------------------------------------------------- #
# Signature verification
# --------------------------------------------------------------------------- #


def test_valid_signature_accepted() -> None:
    body = b'{"hello":"world"}'
    assert verify_signature(
        body=body, header=sign(body), secret=SecretStr(APP_SECRET)
    )


def test_signature_over_different_body_rejected() -> None:
    assert not verify_signature(
        body=b'{"hello":"tampered"}',
        header=sign(b'{"hello":"world"}'),
        secret=SecretStr(APP_SECRET),
    )


def test_signature_with_wrong_secret_rejected() -> None:
    body = b"{}"
    assert not verify_signature(
        body=body, header=sign(body, "other-secret"), secret=SecretStr(APP_SECRET)
    )


@pytest.mark.parametrize(
    "header", [None, "", "deadbeef", "sha1=deadbeef", "sha256=", "sha256=nothex"]
)
def test_malformed_signature_headers_rejected(header: str | None) -> None:
    assert not verify_signature(body=b"{}", header=header, secret=SecretStr(APP_SECRET))


# --------------------------------------------------------------------------- #
# Payload parsing
# --------------------------------------------------------------------------- #


def test_text_message_parsed() -> None:
    parsed, skipped = parse_webhook(envelope(text_message()))

    assert skipped == 0
    assert len(parsed) == 1
    record = parsed[0].record
    assert record.source == "whatsapp"
    assert record.source_id == "wamid.TEXT1"
    assert record.kind == "text"
    assert record.published_at is not None
    assert parsed[0].text == BODY_TEXT


def test_message_text_is_not_stored_on_the_item() -> None:
    """Item rows are metadata only — the body travels separately."""
    parsed, _ = parse_webhook(envelope(text_message()))
    record = parsed[0].record

    assert BODY_TEXT not in json.dumps(record.meta)
    assert record.title is None


def test_contact_profile_name_becomes_author() -> None:
    parsed, _ = parse_webhook(
        envelope(
            text_message(),
            contacts=[{"wa_id": "254700000000", "profile": {"name": "David"}}],
        )
    )
    assert parsed[0].record.author == "David"


def test_author_falls_back_to_sender_id() -> None:
    parsed, _ = parse_webhook(envelope(text_message()))
    assert parsed[0].record.author == "254700000000"


def test_image_with_caption_parsed() -> None:
    parsed, _ = parse_webhook(envelope(media_message(caption="look at this")))
    record = parsed[0].record

    assert record.kind == "image"
    assert parsed[0].text == "look at this"


def test_a_media_id_is_kept_as_metadata_not_as_a_blob_ref() -> None:
    """``raw_ref`` means "a blob ref" (docs/schema.md).

    A Meta media id is a pointer into someone else's API that no extractor can
    open, so putting it in raw_ref made the column mean two different things
    and left every reader guessing which one it held. The pipeline sets raw_ref
    once the bytes are actually in the store.
    """
    parsed, _ = parse_webhook(envelope(media_message()))
    record = parsed[0].record

    assert record.raw_ref is None, "no blob exists yet"
    assert record.meta["wa_media_id"] == "media-abc"


def test_media_without_caption_has_no_text() -> None:
    parsed, _ = parse_webhook(envelope(media_message(kind="audio")))
    assert parsed[0].record.kind == "audio"
    assert parsed[0].text is None


def test_sticker_maps_to_image_kind() -> None:
    parsed, _ = parse_webhook(envelope(media_message(kind="sticker")))
    assert parsed[0].record.kind == "image"


@pytest.mark.parametrize("message_type", ["document", "location", "contacts", "reaction"])
def test_unhandled_types_are_skipped_not_failed(message_type: str) -> None:
    message = {
        "from": "254700000000",
        "id": "wamid.OTHER",
        "timestamp": "1753800000",
        "type": message_type,
        message_type: {"whatever": True},
    }
    parsed, skipped = parse_webhook(envelope(message))

    assert parsed == []
    assert skipped == 1


def test_status_receipts_are_skipped() -> None:
    """Delivery/read receipts arrive on the same endpoint as messages."""
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "entry-1",
                "changes": [
                    {"field": "messages", "value": {"statuses": [{"status": "read"}]}}
                ],
            }
        ],
    }
    parsed, skipped = parse_webhook(payload)

    assert parsed == []
    assert skipped == 0


def test_multiple_entries_and_changes_are_flattened() -> None:
    payload = envelope(text_message(message_id="wamid.A"), text_message(message_id="wamid.B"))
    payload["entry"].append(envelope(text_message(message_id="wamid.C"))["entry"][0])

    parsed, _ = parse_webhook(payload)

    assert [p.record.source_id for p in parsed] == ["wamid.A", "wamid.B", "wamid.C"]


def test_junk_timestamp_becomes_none() -> None:
    message = text_message()
    message["timestamp"] = "not-a-number"
    parsed, _ = parse_webhook(envelope(message))

    assert parsed[0].record.published_at is None


@pytest.mark.parametrize(
    "payload", [{}, {"entry": None}, {"entry": ["junk"]}, {"entry": [{"changes": "junk"}]}]
)
def test_structurally_broken_payloads_do_not_raise(payload: dict[str, Any]) -> None:
    parsed, _ = parse_webhook(payload)
    assert parsed == []


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #


def test_signed_webhook_ingests_and_enqueues(
    client: TestClient, repo: FakeItemRepository, queue: FakeQueue
) -> None:
    response = post(client, envelope(text_message()))

    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "received": 1,
        "accepted": 1,
        "queued": 1,
        "skipped": 0,
    }
    assert len(repo.calls) == 1
    assert queue.jobs[0][1] == BODY_TEXT


def test_unsigned_request_is_rejected_without_touching_the_database(
    client: TestClient, repo: FakeItemRepository, queue: FakeQueue
) -> None:
    body = json.dumps(envelope(text_message())).encode()
    response = client.post("/webhook/whatsapp", content=body)

    assert response.status_code == 403
    assert repo.calls == []
    assert queue.jobs == []


def test_bad_signature_is_rejected(
    client: TestClient, repo: FakeItemRepository
) -> None:
    response = post(client, envelope(text_message()), secret="wrong-secret")

    assert response.status_code == 403
    assert repo.calls == []


def test_malformed_json_with_valid_signature_is_a_400(client: TestClient) -> None:
    body = b"{not json"
    response = client.post(
        "/webhook/whatsapp", content=body, headers={SIGNATURE_HEADER: sign(body)}
    )
    assert response.status_code == 400


def test_non_object_payload_is_a_400(client: TestClient) -> None:
    body = b"[1, 2, 3]"
    response = client.post(
        "/webhook/whatsapp", content=body, headers={SIGNATURE_HEADER: sign(body)}
    )
    assert response.status_code == 400


def test_retried_delivery_does_not_requeue_work(
    client: TestClient, queue: FakeQueue
) -> None:
    """Meta retries aggressively; a duplicate must cost one no-op insert."""
    payload = envelope(text_message())

    first = post(client, payload)
    second = post(client, payload)

    assert first.json()["queued"] == 1
    assert second.json()["queued"] == 0
    assert second.json()["accepted"] == 1
    assert len(queue.jobs) == 1


def test_work_is_committed_before_it_is_enqueued(
    client: TestClient, events: list[str]
) -> None:
    """A worker claiming the job while the insert is uncommitted finds no row.

    Ordering here is the whole fix — relying on dependency teardown to commit
    put the write *after* the enqueue, which races under load.
    """
    post(client, envelope(text_message(message_id="wamid.A"), text_message(message_id="wamid.B")))

    assert events == ["commit", "enqueue", "enqueue"]


def test_webhook_logs_no_message_content(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        post(client, envelope(text_message()))

    emitted = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert BODY_TEXT not in emitted


# --------------------------------------------------------------------------- #
# Subscription handshake
# --------------------------------------------------------------------------- #


def test_handshake_echoes_challenge(client: TestClient) -> None:
    response = client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1158201444",
        },
    )
    assert response.status_code == 200
    assert response.text == "1158201444"


@pytest.mark.parametrize(
    ("mode", "token"),
    [("subscribe", "wrong"), ("unsubscribe", VERIFY_TOKEN), ("subscribe", None)],
)
def test_handshake_rejects_bad_credentials(
    client: TestClient, mode: str, token: str | None
) -> None:
    params: dict[str, str] = {"hub.mode": mode, "hub.challenge": "123"}
    if token is not None:
        params["hub.verify_token"] = token

    assert client.get("/webhook/whatsapp", params=params).status_code == 403
