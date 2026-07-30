"""Substack (and any RSS/Atom) source connector.

Polls one or more feeds and yields a :class:`RawRecord` per entry. Records are
``kind="link"`` carrying a URL, not the post body: the feed's own summary is
usually a truncated teaser, and the pipeline's article extractor recovers the
full text from the URL afterwards. Storing the teaser would classify posts on
their first paragraph.

Deduplication is the database's job, as everywhere else — ``(source,
source_id)`` is unique, so this re-yields entries it has already seen rather
than keeping a local watermark that can drift.

Feeds are third-party documents. Every field is optional, badly typed, or
missing in practice, so parsing degrades entry by entry: one malformed item
must not cost the rest of the feed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, Final, Protocol
from xml.etree.ElementTree import Element, ParseError

from catchment.ingestion.base import RawRecord
from catchment.logging_config import get_logger, log_context

logger = get_logger(__name__)

SOURCE: Final[str] = "substack"
ITEM_KIND: Final[str] = "link"

_TIMEOUT_SECONDS: Final[int] = 30
_MAX_FEED_BYTES: Final[int] = 8 * 1024 * 1024

#: Atom lives in a namespace; RSS does not. Handling both keeps this connector
#: useful for any feed, which is most of what makes it worth having.
_ATOM: Final[str] = "{http://www.w3.org/2005/Atom}"


class FeedError(RuntimeError):
    """Raised when a feed could not be fetched or parsed at all."""


class Http(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any:
        ...


class SubstackConnector:
    """Polls RSS/Atom feeds. One connector, many feeds.

    A plain class rather than a frozen dataclass: the ``Connector`` protocol
    declares ``source`` as a settable attribute, which a frozen dataclass field
    is not. Nothing here mutates after construction regardless.
    """

    source: str = SOURCE

    def __init__(self, feeds: Sequence[str], *, http: Http | None = None) -> None:
        self.feeds = tuple(feeds)
        self.http = http

    def fetch(self) -> Iterable[RawRecord]:
        """Yield every entry from every configured feed.

        A feed that fails is logged and skipped. One dead publication must not
        stop the others from being ingested — that is the difference between a
        degraded poll and a lost one.
        """
        client = self.http if self.http is not None else _default_http()

        for feed_url in self.feeds:
            # Parsing is inside the guard, not just fetching. A site serving an
            # HTML error page with a 200 is ordinary, and it fails at parse
            # time — outside the guard it would kill the whole poll rather than
            # skipping one feed.
            try:
                records = parse_feed(_fetch_feed(client, feed_url), feed_url=feed_url)
            except FeedError as error:
                logger.warning(
                    "feed unavailable; skipping",
                    extra=log_context(feed=feed_url, error=type(error).__name__),
                )
                continue

            logger.info(
                "feed polled", extra=log_context(feed=feed_url, entries=len(records))
            )
            yield from records


# --------------------------------------------------------------------------- #
# Pure parsing — the interesting logic, testable without a network
# --------------------------------------------------------------------------- #


def parse_feed(document: str, *, feed_url: str = "") -> list[RawRecord]:
    """Parse an RSS or Atom document into records.

    Raises :class:`FeedError` only when the document is not XML at all. An
    individual entry that cannot be read is dropped with a log line, because a
    single malformed post should not discard the rest of the feed.
    """
    # defusedxml, not the stdlib parser: a feed is a document written by
    # someone else, and stdlib ElementTree will happily expand an entity bomb
    # that turns a 1KB response into gigabytes of memory.
    from defusedxml.ElementTree import fromstring

    try:
        root = fromstring(document)
    except (ParseError, ValueError) as error:
        # defusedxml raises its own EntityDeclared/DTDForbidden types, which
        # subclass ValueError — a hostile feed is refused like a malformed one.
        raise FeedError(f"feed is not valid XML: {type(error).__name__}") from None

    records: list[RawRecord] = []
    for entry in _entries(root):
        record = _to_record(entry, feed_url=feed_url)
        if record is not None:
            records.append(record)
    return records


def _entries(root: Element) -> Iterator[Element]:
    """Yield entry elements from either dialect."""
    yield from root.iter("item")
    yield from root.iter(f"{_ATOM}entry")


def _to_record(entry: Element, *, feed_url: str) -> RawRecord | None:
    """Convert one entry, or None if it carries nothing usable."""
    url = _link(entry)
    title = _text(entry, "title") or _text(entry, f"{_ATOM}title")

    # A guid is preferred but frequently absent or non-unique. The URL is the
    # better fallback than the title: two posts can share a title, and a title
    # can be edited after publication, which would re-ingest the post.
    source_id = _text(entry, "guid") or _text(entry, f"{_ATOM}id") or url
    if not source_id:
        if not title:
            return None
        # Last resort. Hashed with the feed so two publications posting the
        # same title do not collide on one item.
        source_id = hashlib.sha256(f"{feed_url}:{title}".encode()).hexdigest()

    meta: dict[str, Any] = {"feed": feed_url}
    if summary := _summary(entry):
        # Kept as metadata, never as the extraction: it is a teaser, and the
        # article extractor recovers the real text from the URL.
        meta["has_summary"] = True
        meta["summary_chars"] = len(summary)

    return RawRecord(
        source=SOURCE,
        source_id=source_id[:512],
        kind=ITEM_KIND,
        url=url,
        title=title,
        author=_author(entry),
        published_at=_published(entry),
        meta=meta,
    )


def _text(entry: Element, tag: str) -> str | None:
    element = entry.find(tag)
    if element is None or element.text is None:
        return None
    return element.text.strip() or None


def _link(entry: Element) -> str | None:
    """Find the entry's URL in either dialect.

    RSS puts it in the element text; Atom puts it in an ``href`` attribute and
    may carry several, only one of which is the post.
    """
    if rss := _text(entry, "link"):
        return rss

    for element in entry.findall(f"{_ATOM}link"):
        rel = element.get("rel", "alternate")
        href = element.get("href")
        if rel == "alternate" and href:
            return href.strip() or None
    return None


def _author(entry: Element) -> str | None:
    if direct := _text(entry, "author"):
        return direct
    if (atom := entry.find(f"{_ATOM}author")) is not None:
        name = atom.find(f"{_ATOM}name")
        if name is not None and name.text:
            return name.text.strip() or None
    if creator := _text(entry, "{http://purl.org/dc/elements/1.1/}creator"):
        return creator
    return None


def _summary(entry: Element) -> str | None:
    for tag in (
        "description",
        f"{_ATOM}summary",
        "{http://purl.org/rss/1.0/modules/content/}encoded",
    ):
        if value := _text(entry, tag):
            return value
    return None


def _published(entry: Element) -> datetime | None:
    """Read the publication date, tolerating both formats and neither.

    An unparseable date costs the timestamp, never the entry: a post with no
    usable date is still worth having.
    """
    if rfc := _text(entry, "pubDate"):
        try:
            from email.utils import parsedate_to_datetime

            parsed = parsedate_to_datetime(rfc)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    for tag in (f"{_ATOM}published", f"{_ATOM}updated"):
        if iso := _text(entry, tag):
            try:
                parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            except ValueError:
                continue
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _fetch_feed(client: Http, url: str) -> str:
    try:
        response = client.get(url, timeout=_TIMEOUT_SECONDS, follow_redirects=True)
        response.raise_for_status()
    except Exception as error:
        # Client messages routinely embed the full URL; a private feed link is
        # a credential.
        raise FeedError(f"could not fetch feed: {type(error).__name__}") from None

    document = getattr(response, "text", "")
    if not isinstance(document, str) or not document.strip():
        raise FeedError("feed response was empty")
    if len(document) > _MAX_FEED_BYTES:
        raise FeedError("feed exceeded the size limit")
    return document


def _default_http() -> Http:
    import httpx

    return httpx.Client(  # type: ignore[return-value]
        timeout=_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": "catchment/1.0 (+personal content pipeline)"},
    )
