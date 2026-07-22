"""Health read tools: typed capabilities over the day-view surface services.

Each tool is a thin, typed wrapper around an application service — no formula
lives here, and the tenant always comes from the tool context, never the input.
These are the read (``side_effect=none``) subset of the canonical registry; they
are model-invocable because reading a health day is not sensitive to replay.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from akunaki.application.recovery_surface import ServedRecoveryService
from akunaki.application.sleep_surface import SleepSurfaceService
from akunaki.application.today_surface import TodaySurfaceService
from akunaki.application.tool_registry import (
    Sensitivity,
    Tool,
    ToolContext,
    ToolRegistry,
)

READ_HEALTH_SCOPE = "read:health"


class DayInput(BaseModel):
    """The local health day a read tool operates on."""

    day: str = Field(min_length=10, max_length=10, description="Local health day, YYYY-MM-DD.")

    def validated_day(self) -> str:
        """Return the day, raising if it is not a real calendar date."""
        date.fromisoformat(self.day)
        return self.day


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


def register_health_tools(
    registry: ToolRegistry,
    *,
    recovery: ServedRecoveryService,
    sleep: SleepSurfaceService,
    today: TodaySurfaceService,
) -> None:
    """Register the read-health tools on a registry, bound to their services."""
    registry.register(get_today_tool(today))
    registry.register(get_recovery_tool(recovery))
    registry.register(get_sleep_tool(sleep))
