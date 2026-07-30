"""Recover article text from a URL.

The first extractor that does real work, and the only one that needs no blob:
the bytes are behind a link, so it fetches the page itself. That makes it the
one piece of extraction that could ship before blob storage existed.

``trafilatura`` does the hard part — telling an article apart from the
navigation, subscribe prompts and footers wrapped around it. That distinction
matters more than it sounds: boilerplate is identical across every page from a
publication, so leaving it in makes two unrelated articles from one site look
like neighbours in embedding space.

The page is written by someone else. It is untrusted on the same footing as a
WhatsApp message, so it is size-bounded here rather than at the prompt.
"""

from __future__ import annotations

from typing import Any, Final, Protocol
from urllib.parse import urlparse

from catchment.extraction import ExtractionResult
from catchment.logging_config import get_logger, log_context

logger = get_logger(__name__)

EXTRACTOR: Final[str] = "trafilatura"

#: Hard ceiling on recovered text. The classifier truncates for its own prompt
#: budget, but that happens after the text is stored and embedded — a
#: book-length page would still be written to Postgres and pushed through
#: BGE-M3 first. This bound is about what we are willing to keep.
MAX_ARTICLE_CHARS: Final[int] = 100_000

#: Anything below this is boilerplate, not an article: a cookie wall, a paywall
#: stub, a "you must enable JavaScript" page.
MIN_ARTICLE_CHARS: Final[int] = 80

_TIMEOUT_SECONDS: Final[int] = 30
_ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})


class ArticleExtractionError(RuntimeError):
    """Raised when a URL could not be turned into article text.

    Callers treat this as an ordinary outcome: a paywalled or dead link should
    leave a reviewable item, not a failed job.
    """


class Http(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any:
        ...


def extract_article(url: str, *, http: Http | None = None) -> ExtractionResult:
    """Fetch ``url`` and return its article text."""
    target = _validate_url(url)
    client = http if http is not None else _default_http()

    html = _fetch(client, target)
    text, title = _parse(html, url=target)

    truncated = len(text) > MAX_ARTICLE_CHARS
    if truncated:
        text = text[:MAX_ARTICLE_CHARS].rstrip() + "\n[truncated]"

    logger.info(
        "article extracted",
        extra=log_context(host=urlparse(target).hostname, chars=len(text)),
    )
    return ExtractionResult(
        extractor=EXTRACTOR,
        text=text,
        meta={"url": target, "title": title, "truncated": truncated},
    )


def _validate_url(url: str) -> str:
    """Reject anything not an ordinary web page.

    The URL arrives inside ingested content, so it is chosen by whoever sent
    the message. ``file://`` would read local disk and hand it to a classifier
    prompt; the other schemes are simply not pages.
    """
    candidate = (url or "").strip()
    if not candidate:
        raise ArticleExtractionError("no url to extract from")

    parsed = urlparse(candidate)
    if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.hostname:
        raise ArticleExtractionError(
            f"url scheme {parsed.scheme or 'missing'!r} is not fetchable"
        )
    return candidate


def _fetch(client: Http, url: str) -> str:
    """Fetch the page, keeping the client's own message out of the exception.

    HTTP client errors routinely embed the full URL, query string included —
    which for a shared read-link is a credential.
    """
    try:
        response = client.get(url, timeout=_TIMEOUT_SECONDS, follow_redirects=True)
        response.raise_for_status()
    except Exception as error:
        raise ArticleExtractionError(
            f"could not fetch article: {type(error).__name__}"
        ) from None

    html = getattr(response, "text", "")
    if not isinstance(html, str) or not html.strip():
        raise ArticleExtractionError("article response was empty")
    return html


def _parse(html: str, *, url: str) -> tuple[str, str | None]:
    """Pull the article body and title out of the page."""
    import trafilatura

    text = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )
    if not text or len(text.strip()) < MIN_ARTICLE_CHARS:
        # A cookie wall or paywall stub returns 200 with no content. An empty
        # extraction would be stored and read as "this article said nothing".
        raise ArticleExtractionError("page carried no article text")

    return text.strip(), _title(html, url=url)


def _title(html: str, *, url: str) -> str | None:
    import trafilatura

    try:
        metadata = trafilatura.extract_metadata(html, default_url=url)
    except Exception:  # noqa: BLE001 - metadata is a nicety, never the point
        return None
    title = getattr(metadata, "title", None) if metadata is not None else None
    return title if isinstance(title, str) and title.strip() else None


def _default_http() -> Http:
    import httpx

    return httpx.Client(  # type: ignore[return-value]
        timeout=_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": "catchment/1.0 (+personal content pipeline)"},
    )
