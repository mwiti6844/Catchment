"""IMAP connector: header parsing, id derivation, batch resilience, no leaks."""

from __future__ import annotations

import imaplib
import logging
from email import message_from_bytes
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from catchment.config import MissingConfiguration, Settings
from catchment.ingestion.base import Connector
from catchment.ingestion.email_imap import (
    ImapConnector,
    ImapError,
    build_connector,
    decode_header_value,
    fallback_source_id,
    has_attachments,
    message_source_id,
    parse_date,
    sender_domain,
    sender_name,
    to_raw_record,
)

FIXTURES = Path(__file__).parent / "fixtures" / "email"

PLAIN_BODY = "The body of this message must never appear in a log line."
PLAIN_SUBJECT = "Catchment hydrology notes"


def load(name: str) -> bytes:
    return (FIXTURES / f"{name}.eml").read_bytes()


def message(name: str) -> Message:
    return message_from_bytes(load(name))


# --------------------------------------------------------------------------- #
# Fake IMAP server
# --------------------------------------------------------------------------- #


class FakeImap:
    """Implements only the handful of methods the connector calls."""

    def __init__(
        self,
        messages: dict[str, bytes] | None = None,
        *,
        login_fails: bool = False,
        select_status: str = "OK",
        search_status: str = "OK",
        broken_uids: set[str] | None = None,
    ) -> None:
        self.messages = messages or {}
        self.login_fails = login_fails
        self.select_status = select_status
        self.search_status = search_status
        self.broken_uids = broken_uids or set()
        self.logged_out = False
        self.selected: str | None = None
        self.fetched: list[str] = []

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        if self.login_fails:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED for " + username)
        return "OK", [b"logged in"]

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.selected = folder
        return self.select_status, [b"1"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        if command == "SEARCH":
            if self.search_status != "OK":
                return self.search_status, [None]
            return "OK", [b" ".join(uid.encode() for uid in self.messages)]

        uid = str(args[0])
        self.fetched.append(uid)
        if uid in self.broken_uids:
            return "OK", [b")"]  # a response with no message payload
        raw = self.messages[uid]
        return "OK", [(f"{uid} (RFC822 {{{len(raw)}}}".encode(), raw), b")"]

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return "BYE", [b"bye"]


@pytest.fixture
def imap_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("CATCHMENT_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("CATCHMENT_IMAP_USERNAME", "me@example.com")
    monkeypatch.setenv("CATCHMENT_IMAP_PASSWORD", "correct-horse-battery")
    monkeypatch.setenv("CATCHMENT_IMAP_FOLDER", "INBOX")
    return Settings()


def connector(server: FakeImap, settings: Settings) -> ImapConnector:
    return ImapConnector(settings, connection_factory=lambda host, port: server)  # type: ignore[arg-type,return-value]


# --------------------------------------------------------------------------- #
# Header parsing
# --------------------------------------------------------------------------- #


def test_plain_subject_and_sender() -> None:
    record = to_raw_record(message("plain"), folder="INBOX", uid="1")

    assert record.source == "email"
    assert record.kind == "text"
    assert record.title == PLAIN_SUBJECT
    assert record.author == "David Muthiru"
    assert record.meta["from_domain"] == "example.com"


def test_rfc2047_encoded_subject_is_decoded() -> None:
    record = to_raw_record(message("encoded_subject"), folder="INBOX", uid="2")

    assert record.title == "Café culture and Kenyan fintech"
    assert record.author == "José García"
    assert record.meta["list_id"] == "Weekly Digest <digest.newsletter.example>"


def test_unknown_charset_degrades_instead_of_raising() -> None:
    record = to_raw_record(message("malformed_headers"), folder="INBOX", uid="5")
    assert record.title == "hello"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("plain text", "plain text"),
        ("=?UTF-8?B?SGVsbG8=?=", "Hello"),
        ("=?utf-8?q?caf=C3=A9?=", "café"),
    ],
)
def test_decode_header_value_cases(raw: str | None, expected: str | None) -> None:
    assert decode_header_value(raw) == expected


def test_date_is_parsed_as_aware() -> None:
    parsed = parse_date("Tue, 15 Jul 2025 09:30:00 +0300")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.hour == 9


@pytest.mark.parametrize("raw", [None, "", "not-a-real-date", "Tue, 32 Zzz 2025"])
def test_malformed_dates_become_none(raw: str | None) -> None:
    assert parse_date(raw) is None


def test_malformed_date_does_not_stop_the_record() -> None:
    record = to_raw_record(message("malformed_headers"), folder="INBOX", uid="5")
    assert record.published_at is None
    assert record.source_id == "<malformed-005@example.com>"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("David <david@Example.COM>", "example.com"),
        ("bare@example.org", "example.org"),
        ("no-at-sign", None),
        (None, None),
    ],
)
def test_sender_domain_extraction(raw: str | None, expected: str | None) -> None:
    assert sender_domain(raw) == expected


def test_sender_name_falls_back_to_address() -> None:
    assert sender_name("reports@example.org") == "reports@example.org"


def test_attachment_detection() -> None:
    assert has_attachments(message("with_attachment")) is True
    assert has_attachments(message("plain")) is False
    assert to_raw_record(message("with_attachment"), folder="INBOX", uid="3").meta[
        "has_attachments"
    ]


def test_body_is_never_copied_into_the_record() -> None:
    """items rows are metadata only; the body belongs in an extractions row."""
    record = to_raw_record(message("plain"), folder="INBOX", uid="1")

    assert PLAIN_BODY not in str(record.meta)
    assert PLAIN_BODY not in (record.title or "")


# --------------------------------------------------------------------------- #
# Source id derivation
# --------------------------------------------------------------------------- #


def test_message_id_is_used_when_present() -> None:
    assert message_source_id(message("plain"), folder="INBOX", uid="1") == (
        "<plain-001@example.com>"
    )


def test_fallback_is_used_when_message_id_is_absent() -> None:
    source_id = message_source_id(message("no_message_id"), folder="INBOX", uid="4")
    assert source_id.startswith("sha256:")


def test_fallback_is_deterministic_across_refetches() -> None:
    first = message_source_id(message("no_message_id"), folder="INBOX", uid="4")
    second = message_source_id(message("no_message_id"), folder="INBOX", uid="4")
    assert first == second


def test_fallback_is_stable_when_the_uid_changes() -> None:
    """UIDs are only unique while UIDVALIDITY holds; keying on one would mint a
    new id for a message we already ingested after a mailbox rebuild."""
    original = message_source_id(message("no_message_id"), folder="INBOX", uid="4")
    after_rebuild = message_source_id(message("no_message_id"), folder="INBOX", uid="991")
    assert original == after_rebuild


def test_fallback_differs_across_messages() -> None:
    one = fallback_source_id(message("no_message_id"), folder="INBOX", uid="4")
    two = fallback_source_id(message("plain"), folder="INBOX", uid="4")
    assert one != two


def test_fallback_differs_across_folders() -> None:
    inbox = fallback_source_id(message("no_message_id"), folder="INBOX", uid="4")
    archive = fallback_source_id(message("no_message_id"), folder="Archive", uid="4")
    assert inbox != archive


def test_headerless_message_falls_back_to_the_uid() -> None:
    empty = message_from_bytes(b"\r\nbody only\r\n")
    one = fallback_source_id(empty, folder="INBOX", uid="7")
    two = fallback_source_id(empty, folder="INBOX", uid="8")
    assert one != two


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #


def test_connector_satisfies_the_protocol(imap_settings: Settings) -> None:
    assert isinstance(connector(FakeImap(), imap_settings), Connector)


def test_fetch_returns_a_record_per_message(imap_settings: Settings) -> None:
    server = FakeImap({"1": load("plain"), "2": load("encoded_subject")})

    records = connector(server, imap_settings).fetch()

    assert [r.source_id for r in records] == [
        "<plain-001@example.com>",
        "<encoded-002@example.com>",
    ]
    assert server.selected == "INBOX"
    assert server.logged_out is True


def test_batch_size_limits_the_fetch(
    imap_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CATCHMENT_IMAP_BATCH_SIZE", "2")
    settings = Settings()
    server = FakeImap({str(i): load("plain") for i in range(1, 6)})

    connector(server, settings).fetch()

    # Most recent messages win when the mailbox exceeds the batch.
    assert server.fetched == ["4", "5"]


def test_one_broken_message_does_not_abort_the_batch(imap_settings: Settings) -> None:
    server = FakeImap(
        {"1": load("plain"), "2": b"", "3": load("with_attachment")},
        broken_uids={"2"},
    )

    records = connector(server, imap_settings).fetch()

    assert [r.source_id for r in records] == [
        "<plain-001@example.com>",
        "<attach-003@example.com>",
    ]


def test_empty_mailbox_is_not_an_error(imap_settings: Settings) -> None:
    assert connector(FakeImap({}), imap_settings).fetch() == []


def test_auth_failure_raises_without_echoing_the_credential(
    imap_settings: Settings,
) -> None:
    server = FakeImap({"1": load("plain")}, login_fails=True)

    with pytest.raises(ImapError) as caught:
        connector(server, imap_settings).fetch()

    assert "correct-horse-battery" not in str(caught.value)
    assert "me@example.com" not in str(caught.value)
    assert server.logged_out is True, "the connection must still be closed"


def test_unopenable_folder_raises(imap_settings: Settings) -> None:
    server = FakeImap({"1": load("plain")}, select_status="NO")

    with pytest.raises(ImapError, match="cannot open folder"):
        connector(server, imap_settings).fetch()


def test_failed_search_raises(imap_settings: Settings) -> None:
    server = FakeImap({"1": load("plain")}, search_status="NO")

    with pytest.raises(ImapError, match="search failed"):
        connector(server, imap_settings).fetch()


def test_unreachable_host_raises(imap_settings: Settings) -> None:
    def refuse(host: str, port: int) -> Any:
        raise OSError("connection refused")

    with pytest.raises(ImapError, match="cannot reach IMAP host"):
        ImapConnector(imap_settings, connection_factory=refuse).fetch()


def test_build_connector_fails_fast_when_unconfigured(settings: Settings) -> None:
    """The autouse fixture leaves IMAP settings unset."""
    with pytest.raises(MissingConfiguration, match="IMAP"):
        build_connector(settings)


# --------------------------------------------------------------------------- #
# Leak checks
# --------------------------------------------------------------------------- #


def test_polling_logs_counts_not_correspondence(
    imap_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakeImap({"1": load("plain"), "2": load("encoded_subject")})

    with caplog.at_level(logging.DEBUG):
        connector(server, imap_settings).fetch()

    emitted = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert PLAIN_BODY not in emitted
    assert PLAIN_SUBJECT not in emitted
    assert "david@example.com" not in emitted
    assert "correct-horse-battery" not in emitted
    assert "folder" in emitted


def test_skip_warning_names_the_uid_only(
    imap_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakeImap({"1": b""}, broken_uids={"1"})

    with caplog.at_level(logging.WARNING):
        connector(server, imap_settings).fetch()

    record = caplog.records[-1]
    assert record.uid == "1"  # type: ignore[attr-defined]
    assert PLAIN_BODY not in str(record.__dict__)
