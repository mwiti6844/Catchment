"""The Substack/RSS connector.

Feeds are third-party documents: every field is optional, badly typed, or
missing somewhere in the wild. So the shape of these tests is mostly "one bad
entry must not cost the rest of the feed", and "a missing field costs that
field, never the post".

The other decision under test is that records are links, not bodies. A feed's
description is a teaser; storing it would classify a post on its first
paragraph. The pipeline's article extractor recovers the real text from the URL.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from catchment.ingestion.substack import (
    SOURCE,
    FeedError,
    SubstackConnector,
    parse_feed,
)

FEED_URL = "https://example.substack.com/feed"

RSS = """<?xml version="1.0"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>Example</title>
    <item>
      <title>Catchment Hydrology</title>
      <link>https://example.substack.com/p/catchment-hydrology</link>
      <guid>https://example.substack.com/p/catchment-hydrology</guid>
      <pubDate>Wed, 29 Jul 2026 09:00:00 GMT</pubDate>
      <dc:creator>David</dc:creator>
      <description>A teaser paragraph that is not the article.</description>
    </item>
    <item>
      <title>Second Post</title>
      <link>https://example.substack.com/p/second</link>
      <guid>guid-second</guid>
    </item>
  </channel>
</rss>
"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example</title>
  <entry>
    <title>Atom Post</title>
    <id>tag:example.com,2026:1</id>
    <link rel="edit" href="https://example.com/edit/1"/>
    <link rel="alternate" href="https://example.com/p/atom-post"/>
    <published>2026-07-29T09:00:00Z</published>
    <author><name>David</name></author>
    <summary>Teaser.</summary>
  </entry>
</feed>
"""


class FakeResponse:
    def __init__(self, text: str = "", status: int = 200) -> None:
        self.text = text
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    def __init__(self, **by_url: Any) -> None:
        self._by_url = by_url
        self.urls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> Any:
        self.urls.append(url)
        result = self._by_url.get(url)
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise RuntimeError("no route")
        return result


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def test_rss_entries_become_records() -> None:
    records = parse_feed(RSS, feed_url=FEED_URL)

    assert len(records) == 2
    first = records[0]
    assert first.source == SOURCE
    assert first.title == "Catchment Hydrology"
    assert first.url == "https://example.substack.com/p/catchment-hydrology"
    assert first.author == "David"


def test_records_are_links_not_bodies() -> None:
    """The feed summary is a teaser. Classifying on it would read the first
    paragraph and call it the post."""
    record = parse_feed(RSS, feed_url=FEED_URL)[0]

    assert record.kind == "link"
    assert record.url is not None
    assert "teaser paragraph" not in str(record.meta), "no body text on the item row"
    assert record.meta["has_summary"] is True


def test_the_publication_date_is_parsed() -> None:
    record = parse_feed(RSS, feed_url=FEED_URL)[0]

    assert record.published_at is not None
    assert record.published_at.year == 2026
    assert record.published_at.tzinfo is not None, "naive timestamps corrupt ordering"


def test_atom_is_handled_too() -> None:
    records = parse_feed(ATOM, feed_url=FEED_URL)

    assert len(records) == 1
    assert records[0].title == "Atom Post"
    assert records[0].author == "David"
    assert records[0].published_at is not None


def test_atom_alternate_link_wins_over_the_others() -> None:
    """An Atom entry carries several links; only one is the post."""
    assert parse_feed(ATOM)[0].url == "https://example.com/p/atom-post"


def test_the_guid_is_the_source_id() -> None:
    assert parse_feed(RSS)[1].source_id == "guid-second"


def test_an_entry_without_a_guid_falls_back_to_its_url() -> None:
    """Not the title: two posts can share one, and an edited title would
    re-ingest the post as though it were new."""
    feed = """<rss><channel><item>
        <title>No Guid</title><link>https://example.com/p/no-guid</link>
    </item></channel></rss>"""

    assert parse_feed(feed)[0].source_id == "https://example.com/p/no-guid"


def test_an_entry_with_neither_guid_nor_url_is_keyed_by_feed_and_title() -> None:
    feed = "<rss><channel><item><title>Only A Title</title></item></channel></rss>"

    first = parse_feed(feed, feed_url="https://a.example/feed")[0]
    second = parse_feed(feed, feed_url="https://b.example/feed")[0]

    assert first.source_id != second.source_id, "two feeds must not collide"


def test_an_entry_with_nothing_usable_is_dropped() -> None:
    feed = "<rss><channel><item><description>orphan</description></item></channel></rss>"

    assert parse_feed(feed) == []


def test_one_bad_entry_does_not_discard_the_feed() -> None:
    feed = """<rss><channel>
        <item><description>no title, no link</description></item>
        <item><title>Good</title><link>https://example.com/p/good</link></item>
    </channel></rss>"""

    records = parse_feed(feed)

    assert [r.title for r in records] == ["Good"]


def test_an_unparseable_date_costs_the_date_not_the_entry() -> None:
    feed = """<rss><channel><item>
        <title>Bad Date</title><link>https://example.com/p/x</link>
        <pubDate>last Tuesday-ish</pubDate>
    </item></channel></rss>"""

    record = parse_feed(feed)[0]

    assert record.title == "Bad Date"
    assert record.published_at is None


def test_a_document_that_is_not_xml_raises() -> None:
    with pytest.raises(FeedError, match="not valid XML"):
        parse_feed("<html>this is a 404 page</html>{{{")


def test_a_very_long_source_id_is_truncated_to_the_column() -> None:
    long_guid = "x" * 900
    feed = f"<rss><channel><item><title>T</title><guid>{long_guid}</guid></item></channel></rss>"

    assert len(parse_feed(feed)[0].source_id) <= 512


# --------------------------------------------------------------------------- #
# Polling
# --------------------------------------------------------------------------- #


def test_every_configured_feed_is_polled() -> None:
    http = FakeHttp(
        **{
            "https://a.example/feed": FakeResponse(RSS),
            "https://b.example/feed": FakeResponse(ATOM),
        }
    )
    connector = SubstackConnector(
        feeds=["https://a.example/feed", "https://b.example/feed"], http=http
    )

    records = list(connector.fetch())

    assert len(records) == 3
    assert len(http.urls) == 2


def test_one_dead_feed_does_not_stop_the_others() -> None:
    """A degraded poll beats a lost one: one publication going down must not
    silently stop ingestion from every other."""
    http = FakeHttp(
        **{
            "https://dead.example/feed": RuntimeError("connection refused"),
            "https://ok.example/feed": FakeResponse(RSS),
        }
    )
    connector = SubstackConnector(
        feeds=["https://dead.example/feed", "https://ok.example/feed"], http=http
    )

    records = list(connector.fetch())

    assert len(records) == 2, "the healthy feed still produced its entries"


def test_a_feed_returning_html_is_skipped_not_raised() -> None:
    http = FakeHttp(**{"https://a.example/feed": FakeResponse("<html>404</html>{{")})

    assert list(SubstackConnector(feeds=["https://a.example/feed"], http=http).fetch()) == []


def test_polling_logs_counts_but_no_titles(caplog: pytest.LogCaptureFixture) -> None:
    """A subscription list is personal: what someone reads is content."""
    http = FakeHttp(**{FEED_URL: FakeResponse(RSS)})

    with caplog.at_level(logging.INFO):
        list(SubstackConnector(feeds=[FEED_URL], http=http).fetch())

    emitted = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert "Catchment Hydrology" not in emitted
    assert "entries" in emitted


def test_an_entity_bomb_is_refused() -> None:
    """A feed is a document written by someone else.

    The stdlib parser expands this into gigabytes; defusedxml refuses it. This
    asserts the behaviour rather than trusting the library swap.
    """
    bomb = """<?xml version="1.0"?>
    <!DOCTYPE lolz [
      <!ENTITY lol "lol">
      <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
      <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
    ]>
    <rss><channel><item><title>&lol3;</title></item></channel></rss>"""

    with pytest.raises(FeedError):
        parse_feed(bomb)


def test_an_external_entity_is_refused() -> None:
    """Otherwise a hostile feed reads local files into an item title."""
    xxe = """<?xml version="1.0"?>
    <!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
    <rss><channel><item><title>&xxe;</title></item></channel></rss>"""

    with pytest.raises(FeedError):
        parse_feed(xxe)
