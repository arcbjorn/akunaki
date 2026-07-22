"""Tests for the health read tools over fake surface services."""

from __future__ import annotations

import pytest

from akunaki.application.recovery_surface import RecoverySurface
from akunaki.application.today_surface import TodaySurface
from akunaki.application.tool_registry import Sensitivity, SideEffect, ToolContext
from akunaki.application.tools.health import (
    get_recovery_tool,
    get_sleep_tool,
    get_today_tool,
)
from akunaki.domain.recommendations import (
    ConflictGroup,
    Recommendation,
    Role,
    RuleId,
)
from akunaki.domain.recovery import RecoveryFactor, RecoveryGap, RecoveryStatus
from akunaki.domain.sleep_summary import SleepStatus, SleepSummary
from akunaki.domain.training_label import TrainingLabel

_CTX = ToolContext(tenant_id="tenant-1", user_id="user-1")


def _recovery_surface(day: str) -> RecoverySurface:
    return RecoverySurface(
        local_health_day=day,
        score_code="recovery",
        status=RecoveryStatus.PARTIAL,
        score=72,
        confidence=0.7,
        available_weight=0.6,
        factors=(
            RecoveryFactor(factor_code="hrv", present=True, weight=0.25, magnitude=80.0),
            RecoveryFactor(factor_code="temperature", present=False, weight=0.1, magnitude=0.0),
        ),
        data_gaps=(RecoveryGap(code="missing_hrv_or_resting_hr"),),
        formula_version="general_recovery_v0.1.0",
    )


def _sleep_summary(day: str) -> SleepSummary:
    return SleepSummary(
        local_health_day=day,
        duration_min=420.0,
        target_min=480,
        adherence_pct=87.5,
        debt_14d_min=120.0,
        debt_known_days=14,
        debt_window_days=14,
        debt_status=SleepStatus.COMPLETE,
        debt_is_lower_bound=False,
    )


class _FakeRecovery:
    def recovery_for_day(
        self, *, tenant_id: str, local_health_day: str, target_min: int = 480
    ) -> RecoverySurface:
        return _recovery_surface(local_health_day)


class _FakeSleep:
    def summary_for_day(
        self, *, tenant_id: str, local_health_day: str, target_min: int = 480
    ) -> SleepSummary:
        return _sleep_summary(local_health_day)


class _FakeToday:
    def today_for_day(
        self, *, tenant_id: str, local_health_day: str, target_min: int = 480
    ) -> TodaySurface:
        return TodaySurface(
            local_health_day=local_health_day,
            status="partial",
            recovery=_recovery_surface(local_health_day),
            sleep=_sleep_summary(local_health_day),
            training_label=TrainingLabel.MODERATE,
            ruleset_version="training_label_v0.1.0",
            primary_recommendation=Recommendation(
                rule_id=RuleId.SLEEP_EXTEND_WINDOW,
                role=Role.PRIMARY,
                priority=80,
                conflict_group=ConflictGroup.SLEEP,
            ),
            supporting_recommendations=(),
            data_gaps=(RecoveryGap(code="strain_not_available"),),
            formula_version="general_recovery_v0.1.0",
        )


def test_get_recovery_tool_shapes_output() -> None:
    tool = get_recovery_tool(_FakeRecovery())  # type: ignore[arg-type]
    out = tool.invoke({"day": "2026-07-22"}, _CTX)
    assert out.score == 72
    assert out.status == "partial"
    # Only present factors are exposed.
    assert {f.factor_code for f in out.factors} == {"hrv"}
    assert out.data_gaps == ["missing_hrv_or_resting_hr"]


def test_get_sleep_tool_shapes_output() -> None:
    tool = get_sleep_tool(_FakeSleep())  # type: ignore[arg-type]
    out = tool.invoke({"day": "2026-07-22"}, _CTX)
    assert out.duration_min == 420.0
    assert out.adherence_pct == pytest.approx(87.5)
    assert out.debt_status == "complete"


def test_get_today_tool_shapes_output() -> None:
    tool = get_today_tool(_FakeToday())  # type: ignore[arg-type]
    out = tool.invoke({"day": "2026-07-22"}, _CTX)
    assert out.training_label == "moderate"
    assert out.primary_recommendation == "sleep_extend_window"
    assert out.recovery.score == 72


def test_tools_carry_read_metadata() -> None:
    tool = get_recovery_tool(_FakeRecovery())  # type: ignore[arg-type]
    assert tool.side_effect is SideEffect.NONE
    assert tool.sensitivity is Sensitivity.HEALTH_READ
    assert "read:health" in tool.scopes
    assert tool.requires_confirmation is False


def test_malformed_day_is_rejected() -> None:
    tool = get_recovery_tool(_FakeRecovery())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="month must be"):
        tool.invoke({"day": "2026-13-40"}, _CTX)


def test_tenant_comes_from_context_not_input() -> None:
    # There is no tenant field on the input model; the context supplies it.
    tool = get_recovery_tool(_FakeRecovery())  # type: ignore[arg-type]
    assert "tenant_id" not in tool.input_model.model_fields
