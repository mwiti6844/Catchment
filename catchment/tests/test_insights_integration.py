"""Trend counts behind the Insights page.

This is the first feature whose output is a *claim about you* rather than a
record of what happened, and that is the risk these tests are written against.
The mitigation is that there is no inference here at all: every number is a
count of rows over an explicit window, and every tag carries the ids of the
items it was counted from. If a figure looks wrong you can open the items and
see for yourself.

So the tests are about arithmetic being auditable — half-open windows that
neither double-count nor drop the boundary, totals that count items rather
than assignments, and a sample that really does point at the counted items.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from catchment.storage.insights import (
    InsightsRepository,
    InvalidWindowError,
    TagTrend,
    TrendReport,
)
from catchment.storage.models import Item, ItemTag, Tag

pytestmark = pytest.mark.integration

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
def session(db_session: Session) -> Iterator[Session]:
    yield db_session


@pytest.fixture
def insights(session: Session) -> InsightsRepository:
    return InsightsRepository(session)


def make_tag(session: Session, slug: str) -> Tag:
    tag = Tag(slug=slug, label=slug.title())
    session.add(tag)
    session.flush()
    return tag


def ingest(session: Session, *, days_ago: float, tags: list[Tag], ref: str) -> Item:
    """An item ingested ``days_ago`` before NOW, carrying ``tags``."""
    item = Item(
        source="whatsapp",
        source_id=ref,
        kind="text",
        ingested_at=NOW - timedelta(days=days_ago),
    )
    session.add(item)
    session.flush()
    for tag in tags:
        session.add(ItemTag(item_id=item.id, tag_id=tag.id, confidence=0.9))
    session.flush()
    return item


def by_slug(report: TrendReport) -> dict[str, TagTrend]:
    return {trend.slug: trend for trend in report.tags}


# --------------------------------------------------------------------------- #
# Counting
# --------------------------------------------------------------------------- #


def test_recent_and_prior_windows_are_counted_separately(
    session: Session, insights: InsightsRepository
) -> None:
    """The comparison is the whole point: a raw count cannot tell you whether
    something is rising or just present."""
    hydrology = make_tag(session, "hydrology")
    ingest(session, days_ago=1, tags=[hydrology], ref="a")
    ingest(session, days_ago=2, tags=[hydrology], ref="b")
    ingest(session, days_ago=9, tags=[hydrology], ref="c")

    report = insights.trends(window_days=7, now=NOW)

    trend = by_slug(report)["hydrology"]
    assert trend.recent_count == 2
    assert trend.prior_count == 1
    assert trend.delta == 1


def test_items_older_than_both_windows_are_ignored(
    session: Session, insights: InsightsRepository
) -> None:
    """Otherwise 'the week before' silently means 'all of history', and every
    tag reads as declining."""
    tag = make_tag(session, "archive")
    ingest(session, days_ago=1, tags=[tag], ref="recent")
    ingest(session, days_ago=100, tags=[tag], ref="ancient")

    trend = by_slug(insights.trends(window_days=7, now=NOW))["archive"]

    assert trend.prior_count == 0


def test_the_window_boundary_belongs_to_exactly_one_window(
    session: Session, insights: InsightsRepository
) -> None:
    """Half-open windows. An inclusive pair would count the boundary item in
    both, so a tag with one item could report two."""
    tag = make_tag(session, "boundary")
    ingest(session, days_ago=7, tags=[tag], ref="exactly-seven-days-ago")

    trend = by_slug(insights.trends(window_days=7, now=NOW))["boundary"]

    assert trend.recent_count + trend.prior_count == 1


def test_a_tag_with_nothing_recent_is_omitted(
    session: Session, insights: InsightsRepository
) -> None:
    """The page answers 'what is happening now'. A tag that was busy last month
    and is silent today belongs to history, not to this week."""
    dormant = make_tag(session, "dormant")
    ingest(session, days_ago=9, tags=[dormant], ref="old")

    assert "dormant" not in by_slug(insights.trends(window_days=7, now=NOW))


def test_an_item_with_two_tags_counts_once_towards_the_total(
    session: Session, insights: InsightsRepository
) -> None:
    """Totals are items, not assignments. Summing assignments would report more
    items than were ingested, which is the fastest way to lose trust in a
    number that cannot be checked by eye."""
    first = make_tag(session, "first")
    second = make_tag(session, "second")
    ingest(session, days_ago=1, tags=[first, second], ref="one-item")

    report = insights.trends(window_days=7, now=NOW)

    assert report.total_recent == 1


def test_untagged_items_still_count_towards_the_total(
    session: Session, insights: InsightsRepository
) -> None:
    """A week of unclassified items must not read as a quiet week — that hides
    a broken classifier behind an innocuous-looking number."""
    ingest(session, days_ago=1, tags=[], ref="unclassified")

    assert insights.trends(window_days=7, now=NOW).total_recent == 1


# --------------------------------------------------------------------------- #
# Traceability
# --------------------------------------------------------------------------- #


def test_every_tag_carries_the_items_it_was_counted_from(
    session: Session, insights: InsightsRepository
) -> None:
    """The guard against an unfalsifiable horoscope: each figure links to the
    items behind it, so a claim can be checked rather than believed."""
    tag = make_tag(session, "traceable")
    first = ingest(session, days_ago=1, tags=[tag], ref="a")
    second = ingest(session, days_ago=2, tags=[tag], ref="b")

    trend = by_slug(insights.trends(window_days=7, now=NOW))["traceable"]

    assert set(trend.sample_item_ids) == {first.id, second.id}


def test_the_sample_is_bounded_and_newest_first(
    session: Session, insights: InsightsRepository
) -> None:
    tag = make_tag(session, "busy")
    for day in range(1, 7):
        ingest(session, days_ago=day, tags=[tag], ref=f"day-{day}")

    trend = by_slug(insights.trends(window_days=7, now=NOW, sample=3))["busy"]

    assert len(trend.sample_item_ids) == 3
    newest = ingest(session, days_ago=0.1, tags=[tag], ref="newest")
    refreshed = by_slug(insights.trends(window_days=7, now=NOW, sample=3))["busy"]
    assert refreshed.sample_item_ids[0] == newest.id


def test_the_sample_only_draws_from_the_recent_window(
    session: Session, insights: InsightsRepository
) -> None:
    """A sample containing last month's item would make the recent count look
    wrong to anyone who followed the link."""
    tag = make_tag(session, "mixed")
    recent = ingest(session, days_ago=1, tags=[tag], ref="recent")
    ingest(session, days_ago=9, tags=[tag], ref="older")

    trend = by_slug(insights.trends(window_days=7, now=NOW))["mixed"]

    assert list(trend.sample_item_ids) == [recent.id]


def test_no_item_text_is_returned(
    session: Session, insights: InsightsRepository
) -> None:
    """Insights is a counting page. It has no reason to carry content, and the
    less that leaves the database the smaller the surface to get wrong."""
    tag = make_tag(session, "quiet")
    ingest(session, days_ago=1, tags=[tag], ref="a")

    trend = by_slug(insights.trends(window_days=7, now=NOW))["quiet"]

    fields = set(vars(type(trend))["__slots__"])
    assert not fields & {"text", "title", "author", "preview"}


# --------------------------------------------------------------------------- #
# Ordering and bounds
# --------------------------------------------------------------------------- #


def test_the_busiest_tag_leads(session: Session, insights: InsightsRepository) -> None:
    quiet = make_tag(session, "quiet")
    loud = make_tag(session, "loud")
    ingest(session, days_ago=1, tags=[quiet], ref="q")
    for day in range(1, 4):
        ingest(session, days_ago=day, tags=[loud], ref=f"l{day}")

    report = insights.trends(window_days=7, now=NOW)

    assert [t.slug for t in report.tags][:2] == ["loud", "quiet"]


def test_the_tag_list_is_bounded(
    session: Session, insights: InsightsRepository
) -> None:
    for index in range(6):
        tag = make_tag(session, f"tag-{index}")
        ingest(session, days_ago=1, tags=[tag], ref=f"i{index}")

    assert len(insights.trends(window_days=7, now=NOW, limit=2).tags) == 2


@pytest.mark.parametrize("window_days", [0, -3, 400])
def test_an_unusable_window_is_refused(
    insights: InsightsRepository, window_days: int
) -> None:
    """A zero window divides the feed into two empty halves; a huge one makes
    'prior' reach back further than the corpus exists."""
    with pytest.raises(InvalidWindowError):
        insights.trends(window_days=window_days, now=NOW)


def test_the_windows_are_reported_so_the_numbers_can_be_reproduced(
    session: Session, insights: InsightsRepository
) -> None:
    """Without the boundaries, 'this week' is unfalsifiable by definition."""
    report = insights.trends(window_days=7, now=NOW)

    assert report.window_start == NOW - timedelta(days=7)
    assert report.prior_start == NOW - timedelta(days=14)


def test_an_empty_corpus_reports_zeroes_rather_than_failing(
    insights: InsightsRepository,
) -> None:
    report = insights.trends(window_days=7, now=NOW)

    assert report.tags == []
    assert report.total_recent == 0
