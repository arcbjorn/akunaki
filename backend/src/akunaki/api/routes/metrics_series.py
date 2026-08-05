"""``GET /v1/metrics/{metric}`` — one measured series with baseline context.

Authenticated and tenant-scoped. Shows the user's **own measurements** over a
window, plus the baseline the design requires charts to carry ("confidence,
source, and baseline context visible").

Measurements, not scores: v0.1.0 ships exactly one score code (recovery), so a
per-metric rating would imply a formula nobody accepted. The baseline is
disclosed as center and dispersion — enough to draw a band — never as a
normalized good/bad number.

Unknown days are **omitted**, and the response says how many of the window's
days were known, so a chart can show a gap rather than a measured zero.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.api.app import get_session_factory
from akunaki.api.security import CurrentSession
from akunaki.application.metric_series import (
    MAX_WINDOW_DAYS,
    SUPPORTED_METRICS,
    MetricNotFoundError,
    MetricSeriesService,
)

router = APIRouter(prefix="/v1/metrics", tags=["metrics"])

DEFAULT_WINDOW_DAYS = 30


class MetricPointResponse(BaseModel):
    """One measured day."""

    local_health_day: str
    value: float


class MetricSeriesResponse(BaseModel):
    """A measured series and its baseline context.

    Carries no score: the values are what was measured, and the baseline is
    descriptive context for a chart, not a rating.
    """

    metric: str
    unit: str
    window_days: int = Field(description="Days requested, including unknown ones.")
    known_days: int = Field(description="Days with a measurement; the rest are gaps.")
    coverage_is_partial: bool = Field(
        description="True when the window has unknown days — never zero-filled.",
    )
    points: list[MetricPointResponse]
    baseline_maturity: str = Field(
        description="'insufficient', 'min', or 'mature' over the present samples.",
    )
    baseline_center: float | None = Field(
        description="Median of present samples; null when the baseline is insufficient.",
    )
    baseline_robust_scale: float | None = Field(
        description="Robust dispersion for a band; null when insufficient.",
    )


class SupportedMetricsResponse(BaseModel):
    """The metrics this build exposes."""

    metrics: list[str]


@router.get("", response_model=SupportedMetricsResponse)
def list_metrics(response: Response, session: CurrentSession) -> SupportedMetricsResponse:
    """List the metric names that can be read.

    A client should not have to guess names or discover them by 404.
    """
    response.headers["Cache-Control"] = "private, no-store"
    return SupportedMetricsResponse(metrics=sorted(SUPPORTED_METRICS))


@router.get("/{metric}", response_model=MetricSeriesResponse)
def read_metric(
    metric: str,
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
    window_days: Annotated[
        int,
        Query(ge=1, le=MAX_WINDOW_DAYS, description="How many days back to include."),
    ] = DEFAULT_WINDOW_DAYS,
) -> MetricSeriesResponse:
    """Return one metric's measured series for the caller's tenant."""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        # Required, like every day surface: a local health day belongs to the
        # tenant's timezone, so the server must never guess it from its clock.
        end = date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_day", "message": "day must be YYYY-MM-DD"},
        ) from exc

    days = [(end - timedelta(days=offset)).isoformat() for offset in range(window_days - 1, -1, -1)]
    service = MetricSeriesService(source=FactRepository(session_factory))
    try:
        series = service.series_for(tenant_id=session.tenant_id, metric=metric, days=days)
    except MetricNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail={"code": "unknown_metric", "metric": metric}
        ) from exc

    return MetricSeriesResponse(
        metric=series.metric,
        unit=series.unit,
        window_days=series.window_days,
        known_days=series.known_days,
        coverage_is_partial=series.coverage_is_partial,
        points=[
            MetricPointResponse(local_health_day=point.local_health_day, value=point.value)
            for point in series.points
        ],
        baseline_maturity=series.baseline_maturity,
        baseline_center=series.baseline_center,
        baseline_robust_scale=series.baseline_robust_scale,
    )
