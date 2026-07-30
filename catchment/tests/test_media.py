"""Fetching WhatsApp media into blob storage.

A media message arrives carrying an id, not bytes. Resolving it takes two
authenticated calls to the Graph API and a short-lived URL, which is why this
runs as its own job rather than inline in the webhook: the webhook must answer
Meta in milliseconds, and a download that fails should be retryable without
replaying the delivery.

The privacy rule that shapes most of this: the URL, the token and the bytes are
all sensitive. Only ids, sizes and mime types may be logged.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

import pytest

from catchment.config import MissingConfiguration, Settings
from catchment.ingestion.media import (
    MediaFetchError,
    MediaNotAvailable,
    fetch_media,
)
from catchment.storage.blobs import FilesystemBlobStore

MEDIA_ID = "media-abc-123"
ITEM_ID = uuid.uuid4()
AUDIO = b"OggS\x00fake voice note bytes"
DOWNLOAD_URL = "https://lookaside.fbsbx.com/whatsapp/short-lived?token=SECRET"
#: Not a credential — a stand-in the assertions check never reaches a log.
FAKE_TOKEN = "graph-token"  # noqa: S105


class FakeResponse:
    def __init__(
        self, *, json_body: Any = None, content: bytes = b"", status: int = 200
    ) -> None:
        self._json = json_body
        self.content = content
        self.status_code = status

    def json(self) -> Any:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    """Records what was requested so header handling can be asserted."""

    def __init__(self, *responses: FakeResponse) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, *, headers: dict[str, str], **kwargs: Any) -> FakeResponse:
        self.calls.append((url, headers))
        if not self._responses:
            raise AssertionError("more requests than the test prepared")
        return self._responses.pop(0)


def settings_with_token(tmp_path: Path, *, token: str | None = FAKE_TOKEN) -> Settings:
    # Field names, not env names: init kwargs bypass the env_prefix alias.
    values: dict[str, Any] = {
        "database_url": "postgresql+psycopg://u:p@localhost:5432/db",
        "redis_url": "redis://localhost:6379/0",
        "blob_root": tmp_path,
    }
    if token is not None:
        values["whatsapp_access_token"] = token
    return Settings(**values)


def metadata(**overrides: Any) -> FakeResponse:
    body = {
        "url": DOWNLOAD_URL,
        "mime_type": "audio/ogg; codecs=opus",
        "file_size": len(AUDIO),
        "id": MEDIA_ID,
        **overrides,
    }
    return FakeResponse(json_body=body)


@pytest.fixture
def store(tmp_path: Path) -> FilesystemBlobStore:
    return FilesystemBlobStore(root=tmp_path / "blobs")


def run(
    http: FakeHttp,
    store: FilesystemBlobStore,
    tmp_path: Path,
    *,
    token: str | None = FAKE_TOKEN,
) -> Any:
    return fetch_media(
        media_id=MEDIA_ID,
        item_id=ITEM_ID,
        store=store,
        http=http,
        settings=settings_with_token(tmp_path, token=token),
    )


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #


def test_media_is_resolved_then_downloaded_into_the_store(
    store: FilesystemBlobStore, tmp_path: Path
) -> None:
    http = FakeHttp(metadata(), FakeResponse(content=AUDIO))

    result = run(http, store, tmp_path)

    assert store.open(result.ref) == AUDIO
    assert result.mime_type == "audio/ogg; codecs=opus"
    assert result.size_bytes == len(AUDIO)


def test_both_requests_carry_the_bearer_token(
    store: FilesystemBlobStore, tmp_path: Path
) -> None:
    """The download URL is on a CDN host but is still authenticated.

    Omitting the token on the second call returns an HTML error page with a 200
    on some paths, so the failure would land in the store as if it were media.
    """
    http = FakeHttp(metadata(), FakeResponse(content=AUDIO))

    run(http, store, tmp_path)

    assert len(http.calls) == 2
    for _url, headers in http.calls:
        assert headers["Authorization"] == f"Bearer {FAKE_TOKEN}"


def test_the_key_is_namespaced_by_source_and_item(
    store: FilesystemBlobStore, tmp_path: Path
) -> None:
    """Keyed by item, not by media id: ids are chosen by WhatsApp and a
    collision would silently overwrite another item's media."""
    http = FakeHttp(metadata(), FakeResponse(content=AUDIO))

    result = run(http, store, tmp_path)

    assert result.ref.startswith("blob://whatsapp/")
    assert str(ITEM_ID) in result.ref


def test_the_extension_follows_the_mime_type(
    store: FilesystemBlobStore, tmp_path: Path
) -> None:
    jpeg = b"\xff\xd8jpeg"
    http = FakeHttp(
        metadata(mime_type="image/jpeg", file_size=len(jpeg)),
        FakeResponse(content=jpeg),
    )

    assert run(http, store, tmp_path).ref.endswith(".jpg")


def test_an_unknown_mime_type_still_stores(
    store: FilesystemBlobStore, tmp_path: Path
) -> None:
    """An unrecognised type must not lose the bytes — the extension is a
    convenience, not the record of what this is."""
    http = FakeHttp(
        metadata(mime_type="application/x-unheard-of", file_size=4),
        FakeResponse(content=b"data"),
    )

    result = run(http, store, tmp_path)

    assert store.open(result.ref) == b"data"
    assert result.ref.endswith(".bin")


# --------------------------------------------------------------------------- #
# Failure modes
# --------------------------------------------------------------------------- #


def test_a_missing_token_is_configuration_not_a_fetch_failure(
    store: FilesystemBlobStore, tmp_path: Path
) -> None:
    """Distinct exception types: one needs a deploy, the other a retry."""
    with pytest.raises(MissingConfiguration):
        run(FakeHttp(), store, tmp_path, token=None)


def test_an_expired_media_id_is_not_retryable(
    store: FilesystemBlobStore, tmp_path: Path
) -> None:
    """Meta expires media after a fixed window. Retrying cannot help, so this
    must not look like a transient outage to the queue."""
    http = FakeHttp(FakeResponse(json_body={"error": {"code": 100}}, status=404))

    with pytest.raises(MediaNotAvailable):
        run(http, store, tmp_path)


def test_metadata_without_a_url_fails_clearly(
    store: FilesystemBlobStore, tmp_path: Path
) -> None:
    http = FakeHttp(FakeResponse(json_body={"mime_type": "audio/ogg"}))

    with pytest.raises(MediaFetchError, match="no download url"):
        run(http, store, tmp_path)


def test_an_empty_download_is_refused(
    store: FilesystemBlobStore, tmp_path: Path
) -> None:
    """Storing zero bytes would mark the item fetched and leave OCR and
    transcription with nothing, which reads as 'this media had no content'."""
    http = FakeHttp(metadata(), FakeResponse(content=b""))

    with pytest.raises(MediaFetchError, match="empty"):
        run(http, store, tmp_path)


def test_a_size_mismatch_is_refused(
    store: FilesystemBlobStore, tmp_path: Path
) -> None:
    """A truncated download is worse than a failed one: it looks complete."""
    http = FakeHttp(metadata(file_size=999_999), FakeResponse(content=AUDIO))

    with pytest.raises(MediaFetchError, match="size"):
        run(http, store, tmp_path)


def test_nothing_is_stored_when_the_download_fails(
    store: FilesystemBlobStore, tmp_path: Path
) -> None:
    http = FakeHttp(metadata(), FakeResponse(content=b""))

    with pytest.raises(MediaFetchError):
        run(http, store, tmp_path)

    assert list((tmp_path / "blobs").rglob("*")) == []


# --------------------------------------------------------------------------- #
# Privacy
# --------------------------------------------------------------------------- #


def test_neither_the_token_nor_the_url_nor_the_bytes_are_logged(
    store: FilesystemBlobStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The signed URL is a bearer credential in its own right — anyone holding
    it can fetch the media until it expires."""
    http = FakeHttp(metadata(), FakeResponse(content=AUDIO))

    with caplog.at_level(logging.DEBUG):
        run(http, store, tmp_path)

    emitted = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert FAKE_TOKEN not in emitted
    assert DOWNLOAD_URL not in emitted
    assert "lookaside" not in emitted
    assert AUDIO.decode("latin-1") not in emitted
    assert MEDIA_ID in emitted, "the id is metadata and is what makes this traceable"
