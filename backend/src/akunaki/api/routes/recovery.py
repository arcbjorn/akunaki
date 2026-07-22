"""The ``/v1/recovery`` recovery surface.

Authenticated and tenant-scoped: the tenant comes from the validated session,
never a client parameter. Recovery is the only shipping 0-100 score
(``general_recovery_v0.1.0``). When the sufficiency gate fails the response is
``insufficient`` with a null score and a disclosed ``data_gaps`` list — never a
fabricated midpoint. For any current tenant that is the expected outcome, since
no HRV/RHR fact source exists yet.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.checkin_repository import CheckInRepository
from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.adapters.db.score_repository import ScoreRepository
from akunaki.api.app import get_session_factory
from akunaki.api.security import CurrentSession
from akunaki.application.recovery_inputs import RecoveryInputService
from akunaki.application.recovery_surface import (
    RecoverySurfaceService,
    ServedRecoveryService,
)

router = APIRouter(prefix="/v1/recovery", tags=["recovery"])


class RecoveryFactorResponse(BaseModel):
    """One present contributor to the recovery composite."""

    factor_code: str
    weight: float
    magnitude: float = Field(description="The component's 0-100 score.")


class RecoveryDataGapResponse(BaseModel):
    """A disclosed reason the score is incomplete or withheld."""

    code: str


class RecoveryResponse(BaseModel):
    """The recovery view for one local health day.

    ``score`` is null when the sufficiency gate fails; ``data_gaps`` then names
    why. Recovery is the only score code that ships in v0.1.0.
    """

    local_health_day: str
    score_code: str
    status: str = Field(description="'ok', 'partial', or 'insufficient'.")
    score: int | None = Field(description="0-100 recovery score, or null when insufficient.")
    confidence: float
    available_weight: float
    factors: list[RecoveryFactorResponse]
    data_gaps: list[RecoveryDataGapResponse]
    formula_version: str
    freshness_at: str | None = Field(
        default=None,
        description="UTC RFC3339 when the served score was computed; null if computed on read.",
    )
    version_n: int | None = Field(
        default=None,
        description="Version of the served score; null if computed on read.",
    )


def _recovery_service(
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> ServedRecoveryService:
    # Serve the persisted score; fall back to computing for a day never scored.
    compute = RecoverySurfaceService(
        inputs=RecoveryInputService(
            features=FactRepository(session_factory),
            subjective=CheckInRepository(session_factory),
        )
    )
    return ServedRecoveryService(
        stored=ScoreRepository(session_factory),
        compute=compute,
    )


@router.get("", response_model=RecoveryResponse)
def recovery(
    response: Response,
    session: CurrentSession,
    service: Annotated[ServedRecoveryService, Depends(_recovery_service)],
    day: Annotated[
        str,
        Query(
            min_length=10,
            max_length=10,
            description="Local health day as YYYY-MM-DD.",
        ),
    ],
) -> RecoveryResponse:
    """Return the recovery view for the caller's tenant and day."""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        # Reject anything that is not a real calendar day before touching the DB.
        date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_day", "message": "day must be YYYY-MM-DD"},
        ) from exc

    surface = service.recovery_for_day(
        tenant_id=session.tenant_id,
        local_health_day=day,
    )
    return RecoveryResponse(
        local_health_day=surface.local_health_day,
        score_code=surface.score_code,
        status=surface.status.value,
        score=surface.score,
        confidence=surface.confidence,
        available_weight=surface.available_weight,
        factors=[
            RecoveryFactorResponse(
                factor_code=factor.factor_code,
                weight=factor.weight,
                magnitude=factor.magnitude,
            )
            for factor in surface.factors
            if factor.present
        ],
        data_gaps=[RecoveryDataGapResponse(code=gap.code) for gap in surface.data_gaps],
        formula_version=surface.formula_version,
        freshness_at=surface.freshness_at,
        version_n=surface.version_n,
    )
