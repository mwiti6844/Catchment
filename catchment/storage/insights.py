"""Trend counts over the ingested corpus.

This is the one part of the system whose output is a *claim about the user*
rather than a record of what arrived, and it is built to be falsifiable. There
is no model call here and no scoring heuristic: every figure is a count of rows
between two explicit timestamps, and every tag carries the ids of items it was
counted from. If a number looks wrong you can open those items and check it.

Everything is measured on ``Item.ingested_at``, not on when a tag was assigned.
The question the page answers is "what arrived in my feed this week"; a
reclassification backfill would otherwise register as a week of new interest.

Windows are half-open — ``[start, end)`` — so an item on the boundary is
counted exactly once. Two inclusive ranges would let one item appear in both
halves and a tag with a single item could report two.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import Select, and_, distinct, func, select
from sqlalchemy.orm import Session

from catchment.storage.models import Item, ItemTag, Tag

#: Longest comparison window. Beyond this "the previous period" reaches back
#: further than the corpus has existed, and the comparison stops meaning
#: anything.
MAX_WINDOW_DAYS: Final[int] = 180

#: Default number of items linked per tag. Enough to check a count by eye
#: without turning the page into a second inbox.
DEFAULT_SAMPLE: Final[int] = 5
MAX_SAMPLE: Final[int] = 20

DEFAULT_TREND_LIMIT: Final[int] = 20
MAX_TREND_LIMIT: Final[int] = 100


class InvalidWindowError(ValueError):
    """Raised for a window that cannot produce a meaningful comparison."""


@dataclass(frozen=True, slots=True)
class TagTrend:
    """One tag's activity across the two windows.

    Deliberately carries no item text, titles or authors: this page counts, and
    the less content that leaves the database the smaller the surface to get
    wrong. ``sample_item_ids`` links to the inbox, which is where content
    already lives behind the same loopback boundary.
    """

    tag_id: uuid.UUID
    slug: str
    label: str
    recent_count: int
    prior_count: int
    #: ``recent - prior``. Kept as a difference rather than a ratio: with a
    #: personal corpus, one item against zero is a division by zero dressed up
    #: as a 100% surge.
    delta: int
    sample_item_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True, slots=True)
class TrendReport:
    """Counts for a window, with the boundaries used to produce them."""

    window_days: int
    #: Both windows are ``[start, end)``. Reported so the arithmetic can be
    #: reproduced against the inbox — a figure whose window is unstated is
    #: unfalsifiable by construction.
    window_start: datetime
    window_end: datetime
    prior_start: datetime
    #: Distinct items ingested per window, including unclassified ones. A week
    #: of items that never got tagged must not read as a quiet week; that would
    #: hide a broken classifier behind a reassuring number.
    total_recent: int
    total_prior: int
    tags: list[TagTrend]


class InsightsRepository:
    """Aggregate reads for the Insights page."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def trends(
        self,
        *,
        window_days: int = 7,
        limit: int = DEFAULT_TREND_LIMIT,
        sample: int = DEFAULT_SAMPLE,
        now: datetime | None = None,
    ) -> TrendReport:
        """Compare the last ``window_days`` against the ``window_days`` before.

        ``now`` is injectable so the arithmetic is testable against fixed
        timestamps rather than against the clock.
        """
        if window_days < 1 or window_days > MAX_WINDOW_DAYS:
            raise InvalidWindowError(
                f"window_days must be between 1 and {MAX_WINDOW_DAYS}, got {window_days}"
            )

        end = now or datetime.now(UTC)
        span = timedelta(days=window_days)
        start = end - span
        prior_start = start - span

        recent = self._counts_by_tag(start, end)
        prior = self._counts_by_tag(prior_start, start)
        samples = self._samples(start, end, tags=list(recent), sample=sample)
        labels = self._labels(list(recent))

        bounded = max(1, min(limit, MAX_TREND_LIMIT))
        trends = [
            TagTrend(
                tag_id=tag_id,
                slug=labels[tag_id][0],
                label=labels[tag_id][1],
                recent_count=count,
                prior_count=prior.get(tag_id, 0),
                delta=count - prior.get(tag_id, 0),
                sample_item_ids=tuple(samples.get(tag_id, ())),
            )
            for tag_id, count in recent.items()
            if tag_id in labels
        ]
        # Busiest first, then fastest-rising, then by slug so the order is
        # stable across reloads with identical counts.
        trends.sort(key=lambda t: (-t.recent_count, -t.delta, t.slug))

        return TrendReport(
            window_days=window_days,
            window_start=start,
            window_end=end,
            prior_start=prior_start,
            total_recent=self._total_items(start, end),
            total_prior=self._total_items(prior_start, start),
            tags=trends[:bounded],
        )

    # ----------------------------------------------------------------- #
    # Internals
    # ----------------------------------------------------------------- #

    def _window(self, start: datetime, end: datetime) -> Select[tuple[uuid.UUID]]:
        """Item ids ingested in ``[start, end)``."""
        return select(Item.id).where(
            and_(Item.ingested_at >= start, Item.ingested_at < end)
        )

    def _counts_by_tag(self, start: datetime, end: datetime) -> dict[uuid.UUID, int]:
        """Distinct items per tag in the window.

        ``distinct`` because one item can hold the same tag only once, but the
        join is written so an accidental duplicate assignment cannot inflate a
        count that a reader is being asked to trust.
        """
        stmt = (
            select(ItemTag.tag_id, func.count(distinct(ItemTag.item_id)))
            .where(ItemTag.item_id.in_(self._window(start, end)))
            .group_by(ItemTag.tag_id)
        )
        return {tag_id: count for tag_id, count in self._session.execute(stmt)}

    def _total_items(self, start: datetime, end: datetime) -> int:
        stmt = select(func.count()).select_from(
            self._window(start, end).subquery("window")
        )
        return int(self._session.execute(stmt).scalar_one())

    def _labels(
        self, tag_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[str, str]]:
        if not tag_ids:
            return {}
        stmt = select(Tag.id, Tag.slug, Tag.label).where(Tag.id.in_(tag_ids))
        return {row[0]: (row[1], row[2]) for row in self._session.execute(stmt)}

    def _samples(
        self,
        start: datetime,
        end: datetime,
        *,
        tags: list[uuid.UUID],
        sample: int,
    ) -> dict[uuid.UUID, list[uuid.UUID]]:
        """Newest items per tag inside the recent window.

        Drawn from the same window as the count it illustrates — a sample that
        included an older item would make the count look wrong to anyone who
        followed the link.
        """
        if not tags:
            return {}

        bounded = max(1, min(sample, MAX_SAMPLE))
        stmt = (
            select(ItemTag.tag_id, Item.id, Item.ingested_at)
            .join(Item, Item.id == ItemTag.item_id)
            .where(
                and_(
                    ItemTag.tag_id.in_(tags),
                    Item.ingested_at >= start,
                    Item.ingested_at < end,
                )
            )
            .order_by(ItemTag.tag_id, Item.ingested_at.desc())
        )

        collected: dict[uuid.UUID, list[uuid.UUID]] = {}
        for tag_id, item_id, _ in self._session.execute(stmt):
            bucket = collected.setdefault(tag_id, [])
            if len(bucket) < bounded:
                bucket.append(item_id)
        return collected
