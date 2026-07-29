"""WhatsApp Cloud API connector: signature verification, payload parsing, webhook.

The webhook does the minimum synchronous work — verify, dedupe, enqueue — and
returns. Extraction and classification happen on an RQ worker, because Meta
retries any request it considers slow or failed, and a retry storm against a
transcription model is not a situation worth being in.

Nothing in this module logs message text, captions, or phone numbers.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, SecretStr

from catchment.config import Settings, get_settings
from catchment.dependencies import (
    IngestionUnitOfWork,
    TaskQueue,
    get_ingestion_unit_of_work,
    get_task_queue,
)
from catchment.ingestion.base import RawRecord
from catchment.logging_config import get_logger, log_context

logger = get_logger(__name__)

SOURCE: Final[str] = "whatsapp"
SIGNATURE_HEADER: Final[str] = "X-Hub-Signature-256"
SIGNATURE_PREFIX: Final[str] = "sha256="

#: WhatsApp message types that map onto our item kinds. Everything else
#: (document, location, contacts, reaction, system, interactive) is skipped in
#: this slice rather than coerced into a kind that would misroute extraction.
_MEDIA_KINDS: Final[dict[str, str]] = {
    "image": "image",
    "sticker": "image",
    "audio": "audio",
    "video": "video",
}


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    """One inbound message, split into what we store and what we extract.

    ``text`` is carried separately rather than folded into ``record.meta``
    because ``items`` rows are metadata only — message text belongs in an
    ``extractions`` row.
    """

    record: RawRecord
    text: str | None = None


class WebhookAck(BaseModel):
    """What we tell Meta. Counts only — never message detail."""

    status: str
    received: int
    accepted: int
    queued: int
    skipped: int


def verify_signature(*, body: bytes, header: str | None, secret: SecretStr) -> bool:
    """Verify Meta's ``X-Hub-Signature-256`` header against the raw request body.

    The comparison is constant-time, and the digest must be computed over the
    exact bytes received — re-serialising the parsed JSON would change
    whitespace and key order and never match.
    """
    if not header or not header.startswith(SIGNATURE_PREFIX):
        return False

    provided = header[len(SIGNATURE_PREFIX) :].strip()
    expected = hmac.new(
        secret.get_secret_value().encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(provided, expected)


def _timestamp(value: Any) -> datetime | None:
    """Parse WhatsApp's epoch-seconds timestamp, tolerating junk."""
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError):
        return None


def _contact_names(value: dict[str, Any]) -> dict[str, str]:
    """Map wa_id -> profile name from the payload's contacts block."""
    names: dict[str, str] = {}
    for contact in value.get("contacts") or []:
        if not isinstance(contact, dict):
            continue
        wa_id = contact.get("wa_id")
        profile = contact.get("profile") or {}
        name = profile.get("name") if isinstance(profile, dict) else None
        if isinstance(wa_id, str) and isinstance(name, str):
            names[wa_id] = name
    return names


def _parse_message(
    message: dict[str, Any], *, names: dict[str, str]
) -> ParsedMessage | None:
    """Turn one message object into a record, or None if we do not handle it."""
    source_id = message.get("id")
    message_type = message.get("type")
    if not isinstance(source_id, str) or not isinstance(message_type, str):
        return None

    sender = message.get("from") if isinstance(message.get("from"), str) else None
    meta: dict[str, Any] = {"wa_type": message_type}
    if sender:
        meta["wa_from"] = sender

    kind: str
    raw_ref: str | None
    text: str | None

    if message_type == "text":
        body = (message.get("text") or {}).get("body")
        if not isinstance(body, str):
            return None
        kind, raw_ref, text = "text", None, body

    elif message_type in _MEDIA_KINDS:
        payload = message.get(message_type) or {}
        if not isinstance(payload, dict):
            return None
        kind = _MEDIA_KINDS[message_type]
        media_id = payload.get("id")
        raw_ref = media_id if isinstance(media_id, str) else None
        caption = payload.get("caption")
        text = caption if isinstance(caption, str) else None
        if mime := payload.get("mime_type"):
            meta["mime_type"] = mime

    else:
        return None

    return ParsedMessage(
        record=RawRecord(
            source=SOURCE,
            source_id=source_id,
            kind=kind,
            author=names.get(sender or "", sender),
            published_at=_timestamp(message.get("timestamp")),
            raw_ref=raw_ref,
            meta=meta,
        ),
        text=text,
    )


def parse_webhook(payload: dict[str, Any]) -> tuple[list[ParsedMessage], int]:
    """Flatten a webhook payload into messages we handle.

    Returns ``(parsed, skipped)``. Meta batches multiple entries and changes
    into one request, and statuses (delivered/read receipts) arrive on the same
    endpoint as messages — those are counted as skipped, not treated as errors.
    """
    parsed: list[ParsedMessage] = []
    skipped = 0

    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                skipped += 1
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                skipped += 1
                continue

            names = _contact_names(value)
            for message in value.get("messages") or []:
                if not isinstance(message, dict):
                    skipped += 1
                    continue
                result = _parse_message(message, names=names)
                if result is None:
                    skipped += 1
                else:
                    parsed.append(result)

    return parsed, skipped


router = APIRouter(prefix="/webhook", tags=["ingestion"])


@router.get("/whatsapp", response_class=PlainTextResponse)
def verify_subscription(
    mode: str | None = Query(default=None, alias="hub.mode"),
    token: str | None = Query(default=None, alias="hub.verify_token"),
    challenge: str | None = Query(default=None, alias="hub.challenge"),
    settings: Settings = Depends(get_settings),
) -> str:
    """Meta's subscription handshake: echo the challenge if the token matches."""
    configured = settings.whatsapp_verify_token
    if configured is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="webhook verification is not configured",
        )
    if (
        mode != "subscribe"
        or token is None
        or not hmac.compare_digest(token, configured.get_secret_value())
    ):
        logger.warning("whatsapp subscription handshake rejected")
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="verification failed")

    logger.info("whatsapp subscription verified")
    return challenge or ""


@router.post("/whatsapp", response_model=WebhookAck)
async def receive_webhook(
    request: Request,
    work: IngestionUnitOfWork = Depends(get_ingestion_unit_of_work),
    queue: TaskQueue = Depends(get_task_queue),
    settings: Settings = Depends(get_settings),
) -> WebhookAck:
    """Verify, dedupe and enqueue. Everything slow happens on the worker."""
    secret = settings.require_whatsapp_secret()
    body = await request.body()

    if not verify_signature(
        body=body, header=request.headers.get(SIGNATURE_HEADER), secret=secret
    ):
        logger.warning("whatsapp webhook signature rejected", extra=log_context(bytes=len(body)))
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="payload is not valid JSON"
        ) from None
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="payload must be an object")

    messages, skipped = parse_webhook(payload)
    pending: list[tuple[str, str | None]] = []

    for parsed in messages:
        record = parsed.record
        item, created = work.items.upsert(
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
        # Only new items get work enqueued; a Meta retry of an already-ingested
        # message costs one insert that does nothing.
        if created:
            pending.append((str(item.id), parsed.text))

    # Commit before enqueuing. A worker that claims the job while this
    # transaction is still open would not find the row it was handed.
    work.commit()

    for item_id, text in pending:
        queue.enqueue(item_id=item_id, text=text)
    queued = len(pending)

    ack = WebhookAck(
        status="accepted",
        received=len(messages) + skipped,
        accepted=len(messages),
        queued=queued,
        skipped=skipped,
    )
    logger.info(
        "whatsapp webhook processed",
        extra=log_context(
            received=ack.received, accepted=ack.accepted, queued=queued, skipped=skipped
        ),
    )
    return ack
