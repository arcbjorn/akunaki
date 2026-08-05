"""``GET /v1/trends`` — several metrics over one window.

Multi-metric exploration: one request instead of a client fanning out N calls
to ``/v1/metrics/{metric}``. Built on the **same** service, so a trend and a
single-metric read can never disagree about a value or its baseline.

No cursor. The payload is bounded by the metric registry (eight) times the
capped window, so the honest limit is how many metrics one request may ask for;
a cursor over a fixed set of eight would be ceremony without benefit.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.api.app import get_session_factory
from akunaki.api.routes.metrics_series import (
    DEFAULT_WINDOW_DAYS,
    MAX_TREND_METRICS,
    MetricSeriesResponse,
    series_response,
    window_days_ending,
)
from akunaki.api.security import CurrentSession
from akunaki.application.metric_series import (
    MAX_WINDOW_DAYS,
    MetricNotFoundError,
    MetricSeriesService,
)

router = APIRouter(prefix="/v1/trends", tags=["trends"])


class TrendsResponse(BaseModel):
    """Several metrics over one window, in the order requested."""

    window_days: int
    series: list[MetricSeriesResponse]


@router.get("", response_model=TrendsResponse)
def read_trends(
    response: Response,
    session: CurrentSession,
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    day: Annotated[
        str,
        Query(
            min_length=10,
            max_length=10,
            description="Newest local health day in the window (YYYY-MM-DD).",
        ),
    ],
    metric: Annotated[
        list[str],
        Query(description="Metric to include; repeat for several."),
    ],
    window_days: Annotated[
        int,
        Query(ge=1, le=MAX_WINDOW_DAYS, description="How many days back to include."),
    ] = DEFAULT_WINDOW_DAYS,
) -> TrendsResponse:
    """Return several metrics over one window, for multi-metric exploration.

    Its own router at ``/v1/trends`` rather than ``/v1/metrics/trends``: that is
    the documented path, and nesting it under ``/v1/metrics`` would also make
    ``trends`` shadow a metric name.

    No cursor: the payload is bounded by the metric registry (eight) times the
    capped window, so the honest limit is how many metrics one request may ask
    for. A cursor over a fixed set of eight would be ceremony without benefit.
    """
    response.headers["Cache-Control"] = "private, no-store"
    if not metric:
        raise HTTPException(
            status_code=422,
            detail={"code": "no_metrics", "message": "at least one metric is required"},
        )
    if len(metric) > MAX_TREND_METRICS:
        raise HTTPException(
            status_code=422,
            detail={"code": "too_many_metrics", "limit": MAX_TREND_METRICS},
        )

    days = window_days_ending(day, window_days)
    service = MetricSeriesService(source=FactRepository(session_factory))
    try:
        series = service.trends_for(tenant_id=session.tenant_id, metrics=metric, days=days)
    except MetricNotFoundError as exc:
        # Named explicitly rather than dropped: a shorter list would read as
        # "no data for that metric" instead of "that metric does not exist".
        raise HTTPException(
            status_code=404, detail={"code": "unknown_metric", "metric": exc.args[0]}
        ) from exc

    return TrendsResponse(
        window_days=len(days),
        series=[series_response(item) for item in series],
    )
