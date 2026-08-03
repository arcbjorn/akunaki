"""The ``/v1/anomalies`` surface: active and recently-cleared wellness flags.

Authenticated and tenant-scoped — the tenant comes from the validated session,
never a parameter.

Anomalies are **non-diagnostic**. Each says one metric departed far from the
user's own recent baseline; none names a condition, implies a cause, or advises
treatment. The response therefore carries the signal, its strength, and when it
ran — nothing that reads as a finding.

``/v1/today`` already reduces these to one boolean that floors the training
label. This is where a user can see *which* signal drove that, and since when.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.anomaly_repository import AnomalyRepository
from akunaki.api.app import get_session_factory
from akunaki.api.security import CurrentSession
from akunaki.application.anomalies_surface import (
    DEFAULT_RECENT_DAYS,
    AnomaliesSurfaceService,
)

router = APIRouter(prefix="/v1/anomalies", tags=["anomalies"])

# Bound the lookback so a caller cannot ask for an unbounded scan.
_MAX_RECENT_DAYS = 90


class AnomalyResponse(BaseModel):
    """One tracked anomaly interval.

    Non-diagnostic: a flagged signal, not a finding. The detector's internal
    z-score is deliberately absent — severity is the disclosed strength.
    """

    feature_code: str = Field(
        description=(
            "Which signal was flagged: low_hrv, elevated_rhr, deviant_temperature, "
            "elevated_respiration, low_activity, or short_sleep."
        ),
    )
    severity: str = Field(description="'moderate' or 'high'.")
    started_on: str = Field(description="Local health day the interval opened.")
    ended_on: str | None = Field(
        description="Local health day it cleared; null while still active.",
    )
    is_active: bool
    formula_version: str = Field(description="Detector version that opened it.")


class AnomaliesResponse(BaseModel):
    """Active and recently-cleared anomalies. Empty when none — the common case."""

    anomalies: list[AnomalyResponse]
    window_days: int = Field(description="How far back a cleared anomaly is still listed.")


@router.get("", response_model=AnomaliesResponse)
def list_anomalies(
    response: Response,
    session: CurrentSession,
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    day: Annotated[
        str,
        Query(
            min_length=10,
            max_length=10,
            description="Local health day to measure the window back from (YYYY-MM-DD).",
        ),
    ],
    window_days: Annotated[
        int,
        Query(
            ge=1,
            le=_MAX_RECENT_DAYS,
            description="How far back to include cleared anomalies.",
        ),
    ] = DEFAULT_RECENT_DAYS,
) -> AnomaliesResponse:
    """List the caller's active and recently-cleared anomalies."""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        # Required, like every other day surface: a local health day belongs to
        # the tenant's timezone, so the server must never guess it from its own
        # clock.
        reference = date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_day", "message": "day must be YYYY-MM-DD"},
        ) from exc

    since_day = (reference - timedelta(days=window_days)).isoformat()
    service = AnomaliesSurfaceService(anomalies=AnomalyRepository(session_factory))
    intervals = service.anomalies_for_tenant(
        tenant_id=session.tenant_id,
        since_day=since_day,
    )
    return AnomaliesResponse(
        anomalies=[
            AnomalyResponse(
                feature_code=interval.feature_code,
                severity=interval.severity.value,
                started_on=interval.started_on,
                ended_on=interval.ended_on,
                is_active=interval.is_active,
                formula_version=interval.formula_version,
            )
            for interval in intervals
        ],
        window_days=window_days,
    )
