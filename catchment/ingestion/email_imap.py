"""Email (IMAP) source connector.

Polls a folder and yields one :class:`RawRecord` per message. Bodies are not
touched here — ``items`` rows are metadata only, and the message text belongs
in an ``extractions`` row produced by a later pipeline stage.

Deduplication is the database's job: ``(source, source_id)`` is unique, so this
connector re-yields messages it has already seen rather than maintaining its
own UID high-water mark. That is deliberate — a local watermark is a second
source of truth that drifts, and IMAP UIDs are only stable while the folder's
UIDVALIDITY holds.

Nothing here logs subjects, bodies, sender addresses, or credentials.
"""

from __future__ import annotations

import hashlib
import imaplib
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from email import message_from_bytes
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Final

from catchment.config import MissingConfiguration, Settings, get_settings
from catchment.ingestion.base import RawRecord
from catchment.logging_config import get_logger, log_context

logger = get_logger(__name__)

SOURCE: Final[str] = "email"
ITEM_KIND: Final[str] = "text"

#: Factory so tests can inject a fake without monkeypatching imaplib internals.
ConnectionFactory = Callable[[str, int], imaplib.IMAP4_SSL]


class ImapError(RuntimeError):
    """Raised when the mailbox cannot be reached, opened, or searched."""


def _default_connection(host: str, port: int) -> imaplib.IMAP4_SSL:
    return imaplib.IMAP4_SSL(host, port)


# --------------------------------------------------------------------------- #
# Pure header parsing — the interesting logic, testable without a server
# --------------------------------------------------------------------------- #


def decode_header_value(raw: str | None) -> str | None:
    """Decode an RFC 2047 header (``=?UTF-8?B?...?=``) to text.

    Real subject lines arrive encoded and in charsets that may be mislabelled
    or unknown to Python. Every failure degrades to replacement characters
    rather than raising — one exotic header must not cost us the message.
    """
    if raw is None:
        return None

    try:
        chunks = decode_header(raw)
    except (UnicodeDecodeError, ValueError):
        return raw.strip() or None

    parts: list[str] = []
    for chunk, charset in chunks:
        if not isinstance(chunk, bytes):
            parts.append(chunk)
            continue
        for candidate in (charset, "utf-8", "latin-1"):
            if not candidate:
                continue
            try:
                parts.append(chunk.decode(candidate))
                break
            except (LookupError, UnicodeDecodeError):
                continue
        else:
            parts.append(chunk.decode("utf-8", errors="replace"))

    return "".join(parts).strip() or None


def parse_date(raw: str | None) -> datetime | None:
    """Parse a ``Date:`` header into an aware datetime, or None if unusable.

    Malformed dates are common in the wild; they must not abort ingestion, and
    a naive datetime would be worse than none for ordering.
    """
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def sender_domain(raw: str | None) -> str | None:
    """Return just the domain from a ``From:`` header.

    Only the domain reaches metadata and logs — enough to filter newsletters
    from correspondence without spreading full addresses through the system.
    """
    _, address = parseaddr(raw or "")
    _, _, domain = address.partition("@")
    return domain.lower() or None


def sender_name(raw: str | None) -> str | None:
    """Return the display name from a ``From:`` header, falling back to the address."""
    display, address = parseaddr(raw or "")
    return decode_header_value(display) or address or None


def has_attachments(message: Message) -> bool:
    """Return True if any MIME part is marked as an attachment."""
    if not message.is_multipart():
        return False
    return any(part.get_content_disposition() == "attachment" for part in message.walk())


def fallback_source_id(message: Message, *, folder: str, uid: str) -> str:
    """Derive a stable id for a message with no ``Message-ID`` header.

    Hashed over metadata rather than the UID, because UIDs are only unique
    while UIDVALIDITY holds — keying on one would mint a *new* id for a message
    we already ingested after a mailbox rebuild, defeating the unique
    constraint. Two messages agreeing on folder, date, sender and subject are
    indistinguishable by metadata anyway; the UID is used only when all of
    those are missing.
    """
    components = [
        folder,
        message.get("Date") or "",
        message.get("From") or "",
        message.get("Subject") or "",
    ]
    if not any(component for component in components[1:]):
        components.append(uid)

    digest = hashlib.sha256("\x1f".join(components).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def message_source_id(message: Message, *, folder: str, uid: str) -> str:
    """Return the ``Message-ID``, or a deterministic fallback."""
    raw = message.get("Message-ID")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return fallback_source_id(message, folder=folder, uid=uid)


def to_raw_record(message: Message, *, folder: str, uid: str) -> RawRecord:
    """Build a metadata-only record. The body is deliberately not read."""
    from_header = message.get("From")
    meta: dict[str, Any] = {
        "folder": folder,
        "uid": uid,
        "has_attachments": has_attachments(message),
    }
    if domain := sender_domain(from_header):
        meta["from_domain"] = domain
    if list_id := decode_header_value(message.get("List-Id")):
        meta["list_id"] = list_id

    return RawRecord(
        source=SOURCE,
        source_id=message_source_id(message, folder=folder, uid=uid),
        kind=ITEM_KIND,
        title=decode_header_value(message.get("Subject")),
        author=sender_name(from_header),
        published_at=parse_date(message.get("Date")),
        meta=meta,
    )


def _message_bytes(payload: Sequence[Any]) -> bytes | None:
    """Pull the raw message out of an imaplib FETCH response."""
    for part in payload:
        if isinstance(part, tuple) and len(part) >= 2:
            body = part[1]
            if isinstance(body, bytes | bytearray):
                return bytes(body)
    return None


# --------------------------------------------------------------------------- #
# Connector
# --------------------------------------------------------------------------- #


class ImapConnector:
    """Polls an IMAP folder for recent messages."""

    source: str = SOURCE

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        connection_factory: ConnectionFactory = _default_connection,
    ) -> None:
        self._settings = settings or get_settings()
        self._connect = connection_factory

    def fetch(self) -> list[RawRecord]:
        """Return the most recent messages in the configured folder.

        Returns a list rather than a generator so the connection is always
        closed by the time this returns, regardless of what the caller does.
        """
        host, username, password = self._settings.require_imap()
        folder = self._settings.imap_folder
        batch_size = self._settings.imap_batch_size

        try:
            connection = self._connect(host, self._settings.imap_port)
        except (OSError, imaplib.IMAP4.error) as error:
            raise ImapError(f"cannot reach IMAP host: {type(error).__name__}") from error

        try:
            self._login(connection, username, password.get_secret_value())
            uids = self._search(connection, folder)[-batch_size:]
            records = self._fetch_all(connection, folder=folder, uids=uids)
        finally:
            self._logout(connection)

        logger.info(
            "imap poll complete",
            extra=log_context(folder=folder, examined=len(uids), yielded=len(records)),
        )
        return records

    def _login(self, connection: imaplib.IMAP4_SSL, username: str, password: str) -> None:
        try:
            connection.login(username, password)
        except imaplib.IMAP4.error as error:
            # The exception text can echo the credential back; never include it.
            raise ImapError("IMAP authentication failed") from error

    def _search(self, connection: imaplib.IMAP4_SSL, folder: str) -> list[str]:
        try:
            status, _ = connection.select(folder, readonly=True)
        except imaplib.IMAP4.error as error:
            raise ImapError(f"cannot open folder {folder!r}") from error
        if status != "OK":
            raise ImapError(f"cannot open folder {folder!r}: {status}")

        try:
            # No charset argument: imaplib silently drops a None, and typeshed
            # rejects it outright. "UID SEARCH ALL" is what we want either way.
            status, data = connection.uid("SEARCH", "ALL")
        except imaplib.IMAP4.error as error:
            raise ImapError(f"search failed in folder {folder!r}") from error
        if status != "OK" or not data or data[0] is None:
            raise ImapError(f"search failed in folder {folder!r}: {status}")

        return [uid.decode("ascii", errors="replace") for uid in data[0].split()]

    def _fetch_all(
        self, connection: imaplib.IMAP4_SSL, *, folder: str, uids: Sequence[str]
    ) -> list[RawRecord]:
        records: list[RawRecord] = []
        for uid in uids:
            record = self._fetch_one(connection, folder=folder, uid=uid)
            if record is not None:
                records.append(record)
        return records

    def _fetch_one(
        self, connection: imaplib.IMAP4_SSL, *, folder: str, uid: str
    ) -> RawRecord | None:
        """Fetch and parse one message. A bad message is skipped, not fatal."""
        try:
            status, payload = connection.uid("FETCH", uid, "(RFC822)")
            if status != "OK":
                raise ImapError(f"FETCH returned {status}")
            raw = _message_bytes(payload)
            if raw is None:
                raise ImapError("FETCH returned no message body")
            return to_raw_record(message_from_bytes(raw), folder=folder, uid=uid)
        except (imaplib.IMAP4.error, ImapError, ValueError, TypeError) as error:
            # One malformed message must not cost us the rest of the batch.
            logger.warning(
                "skipped unreadable message",
                extra=log_context(folder=folder, uid=uid, error=type(error).__name__),
            )
            return None

    def _logout(self, connection: imaplib.IMAP4_SSL) -> None:
        try:
            connection.logout()
        except (OSError, imaplib.IMAP4.error):
            logger.warning("imap logout failed", extra=log_context(source=SOURCE))


def build_connector(settings: Settings | None = None) -> ImapConnector:
    """Construct the connector, raising ``MissingConfiguration`` if unconfigured."""
    resolved = settings or get_settings()
    resolved.require_imap()  # fail fast, before any connection is attempted
    return ImapConnector(resolved)


__all__ = [
    "ImapConnector",
    "ImapError",
    "MissingConfiguration",
    "build_connector",
    "decode_header_value",
    "fallback_source_id",
    "has_attachments",
    "message_source_id",
    "parse_date",
    "sender_domain",
    "sender_name",
    "to_raw_record",
]
