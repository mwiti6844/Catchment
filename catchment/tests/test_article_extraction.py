"""Recovering article text from a URL.

The first real extractor. Unlike OCR and transcription it needs no blob: the
bytes are behind a link, so it fetches the page itself.

Two things shape the design. The page is written by someone else, so it is
untrusted input on the same footing as a WhatsApp message — it may be enormous,
may be an error page returned with a 200, and its text goes on to a classifier
prompt. And a link that does not resolve is an ordinary outcome, not an error:
a paywalled or dead URL should leave a reviewable item, not a failed job.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from catchment.extraction.article import (
    MAX_ARTICLE_CHARS,
    ArticleExtractionError,
    extract_article,
)

URL = "https://example.substack.com/p/catchment-hydrology"

HTML = """
<html><head><title>Catchment Hydrology</title></head>
<body>
  <nav>Home About Subscribe</nav>
  <article>
    <h1>Catchment Hydrology</h1>
    <p>A drainage basin collects precipitation and routes it to one outlet.</p>
    <p>Delineating one is the first step in any water balance study.</p>
  </article>
  <footer>Copyright 2026</footer>
</body></html>
"""


class FakeResponse:
    def __init__(self, text: str = "", status: int = 200) -> None:
        self.text = text
        self.content = text.encode()
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.urls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> Any:
        self.urls.append(url)
        if self._error is not None:
            raise self._error
        return self._response


def run(http: FakeHttp, url: str = URL) -> Any:
    return extract_article(url, http=http)


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def test_body_text_is_recovered() -> None:
    result = run(FakeHttp(FakeResponse(HTML)))

    assert "drainage basin collects precipitation" in result.text
    assert result.extractor == "trafilatura"


def test_chrome_is_stripped() -> None:
    """Navigation and footers are the same on every page from a site.

    Left in, they dominate the embedding: two unrelated articles from one
    publication end up neighbours because they share a menu.
    """
    text = run(FakeHttp(FakeResponse(HTML))).text

    assert "Subscribe" not in text
    assert "Copyright 2026" not in text


def test_the_title_is_kept_as_metadata() -> None:
    result = run(FakeHttp(FakeResponse(HTML)))

    assert result.meta["title"] == "Catchment Hydrology"
    assert result.meta["url"] == URL


def test_text_is_truncated_to_a_budget() -> None:
    """A book-length page would blow the classifier's context window."""
    huge = f"<html><body><article><p>{'word ' * 200_000}</p></article></body></html>"

    result = run(FakeHttp(FakeResponse(huge)))

    assert len(result.text) <= MAX_ARTICLE_CHARS + 32
    assert result.meta["truncated"] is True


def test_a_short_article_is_not_marked_truncated() -> None:
    assert run(FakeHttp(FakeResponse(HTML))).meta["truncated"] is False


# --------------------------------------------------------------------------- #
# Failure modes
# --------------------------------------------------------------------------- #


def test_a_page_with_no_article_text_raises() -> None:
    """A cookie wall returns 200 with no content. Storing an empty extraction
    would read as 'this article said nothing'."""
    http = FakeHttp(FakeResponse("<html><body><nav>Menu</nav></body></html>"))

    with pytest.raises(ArticleExtractionError, match="no article text"):
        run(http)


def test_an_http_error_raises() -> None:
    with pytest.raises(ArticleExtractionError):
        run(FakeHttp(FakeResponse("", status=404)))


def test_a_network_failure_raises_without_leaking_the_message() -> None:
    """Client exceptions can carry the full URL including query parameters."""
    http = FakeHttp(error=RuntimeError(f"failed connecting to {URL}?token=SECRET"))

    with pytest.raises(ArticleExtractionError) as caught:
        run(http)

    assert "SECRET" not in str(caught.value)


@pytest.mark.parametrize("url", ["", "   ", "not-a-url", "ftp://files.example.com/x"])
def test_only_http_urls_are_fetched(url: str) -> None:
    """The URL comes from ingested content. file:// would read local disk."""
    http = FakeHttp(FakeResponse(HTML))

    with pytest.raises(ArticleExtractionError, match="url"):
        run(http, url)

    assert http.urls == [], "nothing was fetched"


def test_a_file_url_is_refused() -> None:
    http = FakeHttp(FakeResponse(HTML))

    with pytest.raises(ArticleExtractionError):
        run(http, "file:///etc/passwd")

    assert http.urls == []


# --------------------------------------------------------------------------- #
# Privacy
# --------------------------------------------------------------------------- #


def test_the_article_body_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A saved article is as personal as the decision to save it."""
    with caplog.at_level(logging.DEBUG):
        run(FakeHttp(FakeResponse(HTML)))

    emitted = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert "drainage basin collects precipitation" not in emitted
    assert "chars" in emitted


def test_the_status_code_survives_into_the_error() -> None:
    """"HTTP 403" and "HTTP 404" are different problems — one is a blocked user
    agent, the other a dead link. Without the code they look identical in the
    failure table."""
    import httpx

    request = httpx.Request("GET", URL)
    response = httpx.Response(403, request=request)
    http = FakeHttp(
        error=httpx.HTTPStatusError("blocked", request=request, response=response)
    )

    with pytest.raises(ArticleExtractionError, match="HTTP 403"):
        run(http)


def test_the_url_still_never_reaches_the_error_message() -> None:
    import httpx

    request = httpx.Request("GET", f"{URL}?token=SECRET")
    response = httpx.Response(403, request=request)
    http = FakeHttp(
        error=httpx.HTTPStatusError("blocked", request=request, response=response)
    )

    with pytest.raises(ArticleExtractionError) as caught:
        run(http)

    assert "SECRET" not in str(caught.value)
