"""The composite ``/v1/today`` day view.

Authenticated and tenant-scoped. It carries the shipping blocks — the recovery
score, the sleep summary, the deterministic training label, and the primary /
supporting recommendations — and discloses everything else as gaps. Strain and
activity do not ship in v0.1.0 and are absent by design, not fabricated.
Recovery is the only 0-100 score; the top-level ``status`` mirrors it.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.anomaly_repository import AnomalyRepository
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
from akunaki.application.sleep_surface import SleepSurfaceService
from akunaki.application.today_surface import TodaySurfaceService
from akunaki.domain.recommendations import Recommendation

router = APIRouter(prefix="/v1/today", tags=["today"])


class TodayRecoveryBlock(BaseModel):
    """The recovery block: the only 0-100 score in v0.1.0."""

    score_code: str
    status: str
    score: int | None
    confidence: float
    available_weight: float


class TodaySleepBlock(BaseModel):
    """The sleep block: a deterministic summary, never a sleep score."""

    duration_min: float
    target_min: int
    adherence_pct: float
    debt_14d_min: float
    debt_known_days: int
    debt_status: str


class TodayDataGap(BaseModel):
    """A disclosed reason a block is absent or a score withheld."""

    code: str


class TrainingRecommendationBlock(BaseModel):
    """The deterministic training label (not a numeric readiness score)."""

    label: str = Field(description="'hard', 'moderate', 'light', 'rest', or 'insufficient'.")
    ruleset_version: str


class RecommendationBlock(BaseModel):
    """A selected recommendation."""

    rule_id: str
    role: str
    priority: int


class TodayResponse(BaseModel):
    """The composite day view for one local health day."""

    local_health_day: str
    status: str = Field(description="Mirrors the recovery status.")
    recovery: TodayRecoveryBlock
    sleep: TodaySleepBlock | None = Field(
        description="Absent when the day has no recorded sleep; see data_gaps."
    )
    training_recommendation: TrainingRecommendationBlock
    primary_recommendation: RecommendationBlock | None = Field(
        description="At most one; null when no rule fired."
    )
    supporting_recommendations: list[RecommendationBlock]
    data_gaps: list[TodayDataGap]
    formula_version: str
    freshness_at: str | None = Field(
        default=None,
        description="UTC RFC3339 of the served recovery score; null if computed on read.",
    )
    provenance_url: str | None = Field(
        default=None,
        description=(
            "Opaque lineage handle for the served recovery score; null when the "
            "score was computed on read and has no persisted derivation run."
        ),
    )


def _score_repository(
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> ScoreRepository:
    return ScoreRepository(session_factory)


def _today_service(
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> TodaySurfaceService:
    facts = FactRepository(session_factory)
    inputs = RecoveryInputService(
        features=facts,
        subjective=CheckInRepository(session_factory),
    )
    compute = RecoverySurfaceService(inputs=inputs)
    served = ServedRecoveryService(stored=ScoreRepository(session_factory), compute=compute)
    return TodaySurfaceService(
        recovery=served,
        sleep=SleepSurfaceService(durations=facts),
        anomalies=AnomalyRepository(session_factory),
        # The same input service the recovery score reads load from, so the
        # composite's ACWR ratio and the prior-load component never disagree.
        load=inputs,
        # And the same check-in source, so the composite's symptom burden and
        # the subjective recovery component read one value.
        symptoms=inputs,
    )


@router.get("", response_model=TodayResponse)
def today(
    response: Response,
    session: CurrentSession,
    service: Annotated[TodaySurfaceService, Depends(_today_service)],
    scores: Annotated[ScoreRepository, Depends(_score_repository)],
    day: Annotated[
        str,
        Query(
            min_length=10,
            max_length=10,
            description="Local health day as YYYY-MM-DD.",
        ),
    ],
) -> TodayResponse:
    """Return the composite day view for the caller's tenant and day."""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        # Reject anything that is not a real calendar day before touching the DB.
        date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_day", "message": "day must be YYYY-MM-DD"},
        ) from exc

    surface = service.today_for_day(
        tenant_id=session.tenant_id,
        local_health_day=day,
    )
    token = scores.current_recovery_provenance_token(
        tenant_id=session.tenant_id,
        local_health_day=day,
    )
    provenance_url = f"/v1/provenance/{token}" if token is not None else None
    sleep_block = None
    if surface.sleep is not None:
        sleep_block = TodaySleepBlock(
            duration_min=surface.sleep.duration_min,
            target_min=surface.sleep.target_min,
            adherence_pct=surface.sleep.adherence_pct,
            debt_14d_min=surface.sleep.debt_14d_min,
            debt_known_days=surface.sleep.debt_known_days,
            debt_status=surface.sleep.debt_status.value,
        )
    return TodayResponse(
        local_health_day=surface.local_health_day,
        status=surface.status,
        recovery=TodayRecoveryBlock(
            score_code=surface.recovery.score_code,
            status=surface.recovery.status.value,
            score=surface.recovery.score,
            confidence=surface.recovery.confidence,
            available_weight=surface.recovery.available_weight,
        ),
        sleep=sleep_block,
        training_recommendation=TrainingRecommendationBlock(
            label=surface.training_label.value,
            ruleset_version=surface.ruleset_version,
        ),
        primary_recommendation=(
            _rec_block(surface.primary_recommendation)
            if surface.primary_recommendation is not None
            else None
        ),
        supporting_recommendations=[_rec_block(rec) for rec in surface.supporting_recommendations],
        data_gaps=[TodayDataGap(code=gap.code) for gap in surface.data_gaps],
        formula_version=surface.formula_version,
        freshness_at=surface.recovery.freshness_at,
        provenance_url=provenance_url,
    )


def _rec_block(rec: Recommendation) -> RecommendationBlock:
    return RecommendationBlock(
        rule_id=rec.rule_id.value,
        role=rec.role.value,
        priority=rec.priority,
    )
