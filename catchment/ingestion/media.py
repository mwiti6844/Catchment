"""Fetch WhatsApp media into blob storage.

A media message arrives carrying an id, not bytes, and turning that id into
bytes takes two authenticated Graph API calls: one for metadata (which returns
a short-lived download URL), one for the download itself.

This runs as its own job rather than inline in the webhook. The webhook has to
answer Meta quickly or the delivery is retried, and a download that fails for
its own reasons should be retryable without replaying the whole delivery.

Three things here are credentials and none of them may be logged: the access
token, the download URL (a bearer credential — anyone holding it can fetch the
media until it expires), and the bytes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final, Protocol

from catchment.config import Settings, get_settings
from catchment.logging_config import get_logger, log_context
from catchment.storage.blobs import BlobStore

logger = get_logger(__name__)

GRAPH_HOST: Final[str] = "https://graph.facebook.com"

#: Extensions for the types WhatsApp actually sends. Anything else is stored as
#: ``.bin`` — the extension is a convenience for humans reading the directory,
#: never the record of what a blob is. ``meta.mime_type`` on the item is that.
_EXTENSIONS: Final[dict[str, str]] = {
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/amr": ".amr",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/3gpp": ".3gp",
    "application/pdf": ".pdf",
}


class MediaFetchError(RuntimeError):
    """Raised when media could not be fetched. Retrying may help."""


class MediaNotAvailable(MediaFetchError):
    """Raised when the media is gone rather than temporarily unreachable.

    Meta expires media after a fixed window. Retrying an expired id burns
    quota and never succeeds, so the queue must be able to tell the two apart.
    """


@dataclass(frozen=True, slots=True)
class FetchedMedia:
    """Where the bytes landed, and what they are."""

    ref: str
    mime_type: str | None
    size_bytes: int


class Http(Protocol):
    """The slice of an HTTP client this module uses."""

    def get(self, url: str, *, headers: dict[str, str], **kwargs: Any) -> Any:
        ...


def fetch_media(
    *,
    media_id: str,
    item_id: uuid.UUID,
    store: BlobStore,
    http: Http | None = None,
    settings: Settings | None = None,
) -> FetchedMedia:
    """Resolve ``media_id`` and store its bytes, returning the blob ref.

    Raises :class:`~catchment.config.MissingConfiguration` when no access token
    is set — that needs a deploy, not a retry, and must not be confused with a
    fetch failure.
    """
    resolved = settings or get_settings()
    token = resolved.require_whatsapp_access_token().get_secret_value()
    client = http if http is not None else _default_http(resolved)
    headers = {"Authorization": f"Bearer {token}"}

    url, mime_type, expected_size = _resolve(client, media_id, headers, resolved)
    data = _download(client, url, headers, media_id=media_id)

    if expected_size is not None and len(data) != expected_size:
        # A truncated download is worse than a failed one: it looks complete,
        # and an extractor would happily produce partial text from it.
        raise MediaFetchError(
            f"media {media_id} download size mismatch: "
            f"expected {expected_size} bytes, got {len(data)}"
        )

    ref = store.put(_key(item_id, mime_type), data)
    logger.info(
        "media fetched",
        extra=log_context(
            media_id=media_id,
            item_id=str(item_id),
            mime_type=mime_type,
            bytes=len(data),
        ),
    )
    return FetchedMedia(ref=ref, mime_type=mime_type, size_bytes=len(data))


def _resolve(
    client: Http, media_id: str, headers: dict[str, str], settings: Settings
) -> tuple[str, str | None, int | None]:
    """Ask the Graph API where the bytes are."""
    endpoint = f"{GRAPH_HOST}/{settings.whatsapp_graph_version}/{media_id}"
    try:
        response = client.get(endpoint, headers=headers)
    except Exception as error:
        raise MediaFetchError(
            f"media {media_id} metadata request failed: {type(error).__name__}"
        ) from error

    status = getattr(response, "status_code", 200)
    if status == 404 or status == 410:
        raise MediaNotAvailable(f"media {media_id} is no longer available")
    if status >= 400:
        raise MediaFetchError(f"media {media_id} metadata returned HTTP {status}")

    payload = response.json()
    if not isinstance(payload, dict):
        raise MediaFetchError(f"media {media_id} metadata was not an object")

    url = payload.get("url")
    if not isinstance(url, str) or not url:
        raise MediaFetchError(f"media {media_id} metadata carried no download url")

    mime_type = payload.get("mime_type")
    size = payload.get("file_size")
    return (
        url,
        mime_type if isinstance(mime_type, str) else None,
        size if isinstance(size, int) and not isinstance(size, bool) else None,
    )


def _download(
    client: Http, url: str, headers: dict[str, str], *, media_id: str
) -> bytes:
    """Fetch the bytes. The URL never reaches a log line or an exception."""
    try:
        response = client.get(url, headers=headers)
        response.raise_for_status()
    except Exception as error:
        # The URL is signed and would otherwise ride along in the message.
        raise MediaFetchError(
            f"media {media_id} download failed: {type(error).__name__}"
        ) from None

    data = response.content
    if not isinstance(data, bytes) or not data:
        # Storing nothing would mark the item fetched and leave OCR and
        # transcription with an empty file, reading as "no content here".
        raise MediaFetchError(f"media {media_id} download was empty")
    return data


def _key(item_id: uuid.UUID, mime_type: str | None) -> str:
    """Build the blob key.

    Keyed by item rather than by media id: ids are chosen by WhatsApp, and a
    repeat would silently overwrite another item's media.
    """
    return f"whatsapp/{item_id}{_extension(mime_type)}"


def _extension(mime_type: str | None) -> str:
    if not mime_type:
        return ".bin"
    # "audio/ogg; codecs=opus" -> "audio/ogg"
    base = mime_type.split(";")[0].strip().lower()
    return _EXTENSIONS.get(base, ".bin")


def _default_http(settings: Settings) -> Http:
    import httpx

    return httpx.Client(  # type: ignore[return-value]
        timeout=settings.embedder_timeout_seconds,
        follow_redirects=True,
    )
