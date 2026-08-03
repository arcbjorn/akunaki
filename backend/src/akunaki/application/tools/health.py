"""Health read tools: typed capabilities over the day-view surface services.

Each tool is a thin, typed wrapper around an application service — no formula
lives here, and the tenant always comes from the tool context, never the input.
These are the read (``side_effect=none``) subset of the canonical registry; they
are model-invocable because reading a health day is not sensitive to replay.
"""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel, Field

from akunaki.application.anomalies_surface import (
    DEFAULT_RECENT_DAYS,
    AnomaliesSurfaceService,
)
from akunaki.application.recovery_surface import ServedRecoveryService
from akunaki.application.sleep_surface import SleepSurfaceService
from akunaki.application.today_surface import TodaySurfaceService
from akunaki.application.tool_registry import (
    Sensitivity,
    Tool,
    ToolContext,
    ToolRegistry,
)
from akunaki.application.workouts_surface import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    WorkoutsSurfaceService,
    WorkoutSummary,
)

READ_HEALTH_SCOPE = "read:health"


class DayInput(BaseModel):
    """The local health day a read tool operates on."""

    day: str = Field(min_length=10, max_length=10, description="Local health day, YYYY-MM-DD.")

    def validated_day(self) -> str:
        """Return the day, raising if it is not a real calendar date."""
        date.fromisoformat(self.day)
        return self.day


class AnomalyWindowInput(DayInput):
    """The day to look back from, and how far."""

    window_days: int = Field(
        default=DEFAULT_RECENT_DAYS,
        ge=1,
        le=90,
        description="How far back to include cleared anomalies.",
    )


class AnomalyDTO(BaseModel):
    """One tracked anomaly interval.

    Non-diagnostic: a flagged signal, not a finding. The detector's internal
    z-score is deliberately absent here as it is on ``/v1/anomalies`` — a bare
    z against a private baseline invites over-reading, and a model relaying it
    would compound that.
    """

    feature_code: str
    severity: str
    started_on: str
    ended_on: str | None
    is_active: bool


class AnomaliesOutput(BaseModel):
    """Active and recently-cleared anomalies."""

    anomalies: list[AnomalyDTO]
    window_days: int


class WorkoutPageInput(BaseModel):
    """One page of workouts."""

    limit: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    cursor: str | None = Field(default=None, description="Opaque cursor from a previous page.")


class WorkoutIdInput(BaseModel):
    """Identifies one workout the caller owns."""

    workout_id: str = Field(min_length=1)


class WorkoutDTO(BaseModel):
    """One workout session.

    ``session_load`` is this system's canonical zone-weighted load. There is no
    workout score: v0.1.0 ships one score code, and a model must not be handed
    a number that looks like a second one.
    """

    workout_id: str
    provider: str
    local_health_day: str
    start_utc: str
    end_utc: str
    session_load: float
    zone1_min: float
    zone2_min: float
    zone3_min: float
    zone4_min: float
    zone5_min: float
    total_zone_min: float


class WorkoutsOutput(BaseModel):
    """One page of workouts plus the cursor for the next."""

    workouts: list[WorkoutDTO]
    next_cursor: str | None


def _workout_dto(summary: WorkoutSummary) -> WorkoutDTO:
    return WorkoutDTO(
        workout_id=summary.workout_id,
        provider=summary.provider,
        local_health_day=summary.local_health_day,
        start_utc=summary.start_utc,
        end_utc=summary.end_utc,
        session_load=summary.session_load,
        zone1_min=summary.zone1_min,
        zone2_min=summary.zone2_min,
        zone3_min=summary.zone3_min,
        zone4_min=summary.zone4_min,
        zone5_min=summary.zone5_min,
        total_zone_min=summary.total_zone_min,
    )


class RecoveryFactorDTO(BaseModel):
    """One present contributor to the recovery composite."""

    factor_code: str
    weight: float
    magnitude: float


class RecoveryOutput(BaseModel):
    """The recovery view (mirrors the /v1/recovery surface)."""

    local_health_day: str
    score_code: str
    status: str
    score: int | None
    confidence: float
    available_weight: float
    factors: list[RecoveryFactorDTO]
    data_gaps: list[str]
    formula_version: str


class SleepOutput(BaseModel):
    """The deterministic sleep summary (mirrors the /v1/sleep surface)."""

    local_health_day: str
    duration_min: float
    target_min: int
    adherence_pct: float
    debt_14d_min: float
    debt_known_days: int
    debt_status: str
    formula_version: str


class TodayRecoveryDTO(BaseModel):
    """The recovery block of the composite day view."""

    score_code: str
    status: str
    score: int | None
    confidence: float


class TodayOutput(BaseModel):
    """The composite day view (mirrors the /v1/today surface)."""

    local_health_day: str
    status: str
    recovery: TodayRecoveryDTO
    training_label: str
    ruleset_version: str
    primary_recommendation: str | None
    data_gaps: list[str]
    formula_version: str


def get_recovery_tool(service: ServedRecoveryService) -> Tool[DayInput, RecoveryOutput]:
    """The ``health.get_recovery`` tool over the served recovery surface."""

    def handler(inputs: DayInput, context: ToolContext) -> RecoveryOutput:
        surface = service.recovery_for_day(
            tenant_id=context.tenant_id,
            local_health_day=inputs.validated_day(),
        )
        return RecoveryOutput(
            local_health_day=surface.local_health_day,
            score_code=surface.score_code,
            status=surface.status.value,
            score=surface.score,
            confidence=surface.confidence,
            available_weight=surface.available_weight,
            factors=[
                RecoveryFactorDTO(factor_code=f.factor_code, weight=f.weight, magnitude=f.magnitude)
                for f in surface.factors
                if f.present
            ],
            data_gaps=[g.code for g in surface.data_gaps],
            formula_version=surface.formula_version,
        )

    return Tool(
        name="health.get_recovery",
        input_model=DayInput,
        output_model=RecoveryOutput,
        handler=handler,
        scopes=(READ_HEALTH_SCOPE,),
        sensitivity=Sensitivity.HEALTH_READ,
        model_exposure=True,
        audit="health.get_recovery",
    )


def get_sleep_tool(service: SleepSurfaceService) -> Tool[DayInput, SleepOutput]:
    """The ``health.get_sleep`` tool over the sleep summary surface."""

    def handler(inputs: DayInput, context: ToolContext) -> SleepOutput:
        summary = service.summary_for_day(
            tenant_id=context.tenant_id,
            local_health_day=inputs.validated_day(),
        )
        return SleepOutput(
            local_health_day=summary.local_health_day,
            duration_min=summary.duration_min,
            target_min=summary.target_min,
            adherence_pct=summary.adherence_pct,
            debt_14d_min=summary.debt_14d_min,
            debt_known_days=summary.debt_known_days,
            debt_status=summary.debt_status.value,
            formula_version=summary.formula_version,
        )

    return Tool(
        name="health.get_sleep",
        input_model=DayInput,
        output_model=SleepOutput,
        handler=handler,
        scopes=(READ_HEALTH_SCOPE,),
        sensitivity=Sensitivity.HEALTH_READ,
        model_exposure=True,
        audit="health.get_sleep",
    )


def get_today_tool(service: TodaySurfaceService) -> Tool[DayInput, TodayOutput]:
    """The ``health.get_today`` tool over the composite day view."""

    def handler(inputs: DayInput, context: ToolContext) -> TodayOutput:
        surface = service.today_for_day(
            tenant_id=context.tenant_id,
            local_health_day=inputs.validated_day(),
        )
        primary = surface.primary_recommendation
        return TodayOutput(
            local_health_day=surface.local_health_day,
            status=surface.status,
            recovery=TodayRecoveryDTO(
                score_code=surface.recovery.score_code,
                status=surface.recovery.status.value,
                score=surface.recovery.score,
                confidence=surface.recovery.confidence,
            ),
            training_label=surface.training_label.value,
            ruleset_version=surface.ruleset_version,
            primary_recommendation=primary.rule_id.value if primary is not None else None,
            data_gaps=[g.code for g in surface.data_gaps],
            formula_version=surface.formula_version,
        )

    return Tool(
        name="health.get_today",
        input_model=DayInput,
        output_model=TodayOutput,
        handler=handler,
        scopes=(READ_HEALTH_SCOPE,),
        sensitivity=Sensitivity.HEALTH_READ,
        model_exposure=True,
        audit="health.get_today",
    )


def find_anomalies_tool(
    service: AnomaliesSurfaceService,
) -> Tool[AnomalyWindowInput, AnomaliesOutput]:
    """The ``health.find_anomalies`` tool over the anomalies surface."""

    def handler(inputs: AnomalyWindowInput, context: ToolContext) -> AnomaliesOutput:
        since = (
            date.fromisoformat(inputs.validated_day()) - timedelta(days=inputs.window_days)
        ).isoformat()
        intervals = service.anomalies_for_tenant(
            tenant_id=context.tenant_id,
            since_day=since,
        )
        return AnomaliesOutput(
            anomalies=[
                AnomalyDTO(
                    feature_code=interval.feature_code,
                    severity=interval.severity.value,
                    started_on=interval.started_on,
                    ended_on=interval.ended_on,
                    is_active=interval.is_active,
                )
                for interval in intervals
            ],
            window_days=inputs.window_days,
        )

    return Tool(
        name="health.find_anomalies",
        input_model=AnomalyWindowInput,
        output_model=AnomaliesOutput,
        handler=handler,
        scopes=(READ_HEALTH_SCOPE,),
        sensitivity=Sensitivity.HEALTH_READ,
        model_exposure=True,
        audit="health.find_anomalies",
    )


def get_recent_workouts_tool(
    service: WorkoutsSurfaceService,
) -> Tool[WorkoutPageInput, WorkoutsOutput]:
    """The ``health.get_recent_workouts`` tool over the workouts surface."""

    def handler(inputs: WorkoutPageInput, context: ToolContext) -> WorkoutsOutput:
        page = service.list_for_tenant(
            tenant_id=context.tenant_id,
            limit=inputs.limit,
            cursor=inputs.cursor,
        )
        return WorkoutsOutput(
            workouts=[_workout_dto(item) for item in page.items],
            next_cursor=page.next_cursor,
        )

    return Tool(
        name="health.get_recent_workouts",
        input_model=WorkoutPageInput,
        output_model=WorkoutsOutput,
        handler=handler,
        scopes=(READ_HEALTH_SCOPE,),
        sensitivity=Sensitivity.HEALTH_READ,
        model_exposure=True,
        audit="health.get_recent_workouts",
    )


def get_workout_tool(service: WorkoutsSurfaceService) -> Tool[WorkoutIdInput, WorkoutDTO]:
    """The ``health.get_workout`` tool over the workout detail surface."""

    def handler(inputs: WorkoutIdInput, context: ToolContext) -> WorkoutDTO:
        summary = service.get_for_tenant(
            tenant_id=context.tenant_id,
            workout_id=inputs.workout_id,
        )
        if summary is None:
            # Unknown and cross-tenant are the same failure, so a caller (or a
            # model) cannot probe ids for existence.
            msg = "workout not found"
            raise LookupError(msg)
        return _workout_dto(summary)

    return Tool(
        name="health.get_workout",
        input_model=WorkoutIdInput,
        output_model=WorkoutDTO,
        handler=handler,
        scopes=(READ_HEALTH_SCOPE,),
        sensitivity=Sensitivity.HEALTH_READ,
        model_exposure=True,
        audit="health.get_workout",
    )


def register_health_tools(
    registry: ToolRegistry,
    *,
    recovery: ServedRecoveryService,
    sleep: SleepSurfaceService,
    today: TodaySurfaceService,
    anomalies: AnomaliesSurfaceService | None = None,
    workouts: WorkoutsSurfaceService | None = None,
) -> None:
    """Register the read-health tools on a registry, bound to their services."""
    registry.register(get_today_tool(today))
    registry.register(get_recovery_tool(recovery))
    registry.register(get_sleep_tool(sleep))
    if anomalies is not None:
        registry.register(find_anomalies_tool(anomalies))
    if workouts is not None:
        registry.register(get_recent_workouts_tool(workouts))
        registry.register(get_workout_tool(workouts))
