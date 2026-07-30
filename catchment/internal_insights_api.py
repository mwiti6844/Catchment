"""Routes behind the Insights page.

The page makes claims about what has been dominating the feed, which is a
different kind of output from everything else in this system: it is an
assertion rather than a record. The containment is that the route performs no
inference at all. It reports counts between two stated timestamps and, for
every tag, the ids of items those counts came from. A figure that cannot be
opened and checked does not appear here.

Item ids only — no titles, no text. Content already lives behind the same
loopback boundary on the inbox routes; there is no reason to duplicate it onto
a page whose job is arithmetic.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from catchment.internal_auth import require_internal_token
from catchment.storage.db import session_scope
from catchment.storage.insights import (
    DEFAULT_SAMPLE,
    MAX_TREND_LIMIT,
    MAX_WINDOW_DAYS,
    InsightsRepository,
    InvalidWindowError,
)

router = APIRouter()


class TagTrendView(BaseModel):
    tag_id: uuid.UUID
    slug: str
    label: str
    recent_count: int
    prior_count: int
    #: A difference, not a ratio. On a personal corpus, one item against zero
    #: is a division by zero dressed up as a surge.
    delta: int
    #: The items behind ``recent_count``, newest first. This is what makes the
    #: number falsifiable rather than a horoscope.
    sample_item_ids: list[uuid.UUID]


class TrendReportView(BaseModel):
    window_days: int
    #: Half-open ``[window_start, window_end)``; the prior window is the same
    #: span immediately before. Returned so the arithmetic can be reproduced
    #: against the inbox.
    window_start: str
    window_end: str
    prior_start: str
    #: Distinct items per window, tagged or not. A week of unclassified items
    #: must not read as a quiet week.
    total_recent: int
    total_prior: int
    tags: list[TagTrendView]


@router.get(
    "/insights",
    response_model=TrendReportView,
    dependencies=[Depends(require_internal_token)],
)
def insights(
    window_days: int = Query(default=7, ge=1, le=MAX_WINDOW_DAYS),
    limit: int = Query(default=20, ge=1, le=MAX_TREND_LIMIT),
    sample: int = Query(default=DEFAULT_SAMPLE, ge=1, le=20),
) -> TrendReportView:
    """Tag activity over the last ``window_days`` against the span before it."""
    with session_scope() as session:
        try:
            report = InsightsRepository(session).trends(
                window_days=window_days, limit=limit, sample=sample
            )
        except InvalidWindowError as error:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from None

        return TrendReportView(
            window_days=report.window_days,
            window_start=report.window_start.isoformat(),
            window_end=report.window_end.isoformat(),
            prior_start=report.prior_start.isoformat(),
            total_recent=report.total_recent,
            total_prior=report.total_prior,
            tags=[
                TagTrendView(
                    tag_id=trend.tag_id,
                    slug=trend.slug,
                    label=trend.label,
                    recent_count=trend.recent_count,
                    prior_count=trend.prior_count,
                    delta=trend.delta,
                    sample_item_ids=list(trend.sample_item_ids),
                )
                for trend in report.tags
            ],
        )
