"""The ``/v1/sleep`` deterministic sleep surface.

Authenticated and tenant-scoped: the tenant comes from the validated session,
never a client parameter, so no caller can read another tenant's sleep by
asking. The response is a deterministic summary — duration, target, adherence,
and a disclosed 14-day debt — and deliberately carries no "sleep score". The
design forbids implying such a score exists.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.api.app import get_session_factory
from akunaki.api.security import CurrentSession
from akunaki.application.sleep_surface import SleepSurfaceService
from akunaki.domain.sleep_summary import DEBT_RECOMMENDATION_MIN_KNOWN

router = APIRouter(prefix="/v1/sleep", tags=["sleep"])


class SleepDebtResponse(BaseModel):
    """The disclosed 14-day sleep debt for the day."""

    minutes: float = Field(description="Accumulated debt in minutes.")
    known_days: int = Field(description="Days with known sleep in the 14-day window.")
    window_days: int = Field(description="Days considered (14, or fewer for new users).")
    status: str = Field(description="'complete' when 14/14 known, else 'partial'.")
    is_lower_bound: bool = Field(
        description="True when unknown days mean actual debt could be higher."
    )
    recommendation_eligible: bool = Field(
        description="Whether debt recommendations may be shown (needs >= 12 known days)."
    )


class SleepSummaryResponse(BaseModel):
    """The deterministic sleep summary for one local health day.

    Not a score: this exposes only measured duration against a target and the
    disclosed debt. Clients must not present any of these as a sleep score.
    """

    local_health_day: str
    duration_min: float
    target_min: int
    adherence_pct: float = Field(description="Bounded 0-100; no oversleep bonus.")
    debt: SleepDebtResponse
    formula_version: str


def _sleep_service(
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> SleepSurfaceService:
    return SleepSurfaceService(durations=FactRepository(session_factory))


@router.get("", response_model=SleepSummaryResponse)
def sleep_summary(
    response: Response,
    session: CurrentSession,
    service: Annotated[SleepSurfaceService, Depends(_sleep_service)],
    day: Annotated[
        str,
        Query(
            min_length=10,
            max_length=10,
            description="Local health day as YYYY-MM-DD.",
        ),
    ],
) -> SleepSummaryResponse:
    """Return the deterministic sleep summary for the caller's tenant and day."""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        # Reject anything that is not a real calendar day before touching the DB.
        date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_day", "message": "day must be YYYY-MM-DD"},
        ) from exc

    summary = service.summary_for_day(
        tenant_id=session.tenant_id,
        local_health_day=day,
    )
    return SleepSummaryResponse(
        local_health_day=summary.local_health_day,
        duration_min=summary.duration_min,
        target_min=summary.target_min,
        adherence_pct=summary.adherence_pct,
        debt=SleepDebtResponse(
            minutes=summary.debt_14d_min,
            known_days=summary.debt_known_days,
            window_days=summary.debt_window_days,
            status=summary.debt_status.value,
            is_lower_bound=summary.debt_is_lower_bound,
            recommendation_eligible=summary.debt_known_days >= DEBT_RECOMMENDATION_MIN_KNOWN,
        ),
        formula_version=summary.formula_version,
    )
