"""TodaySurfaceService: ACWR and symptom-burden wiring into the training label.

The composite reads the day's descriptive ACWR from a load source and the
symptom burden from a check-in source, feeding both to the pure training-label
rules. These verify that a real over-load ratio downshifts, that a high symptom
burden floors the label at light, and that absent/undefined inputs (or no source
wired) leave the base label untouched — never a fabricated value.
"""

from __future__ import annotations

from akunaki.application.recovery_surface import RecoverySurface
from akunaki.application.sleep_surface import SleepSurfaceService
from akunaki.application.today_surface import TodaySurfaceService
from akunaki.domain.recommendations import RuleId
from akunaki.domain.recovery import RecoveryFactor, RecoveryStatus
from akunaki.domain.training_label import TrainingLabel

TARGET_DAY = "2026-07-22"


def _hard_recovery(day: str, *, hrv_magnitude: float = 85.0) -> RecoverySurface:
    # A high recovery score -> base label HARD. The HRV component magnitude is
    # tunable: the load_ease recommendation needs a weak HRV (< 40) as well as
    # over-load, so a strong HRV alone must not fire it.
    return RecoverySurface(
        local_health_day=day,
        score_code="recovery",
        status=RecoveryStatus.OK,
        score=90,
        confidence=0.9,
        available_weight=0.9,
        factors=(
            RecoveryFactor(factor_code="hrv", present=True, weight=0.25, magnitude=hrv_magnitude),
            RecoveryFactor(factor_code="resting_hr", present=True, weight=0.20, magnitude=80.0),
        ),
        data_gaps=(),
        formula_version="general_recovery_v0.1.0",
    )


def _rest_recovery(day: str) -> RecoverySurface:
    """A score under the rest band (< 40) with a weak HRV component.

    Both halves matter: the low score drives the REST training label, and the
    weak HRV lets ``load_ease`` fire alongside it so the two collide in the
    load conflict group.
    """
    return RecoverySurface(
        local_health_day=day,
        score_code="recovery",
        status=RecoveryStatus.OK,
        score=25,
        confidence=0.9,
        available_weight=0.9,
        factors=(
            RecoveryFactor(factor_code="hrv", present=True, weight=0.25, magnitude=30.0),
            RecoveryFactor(factor_code="resting_hr", present=True, weight=0.20, magnitude=30.0),
        ),
        data_gaps=(),
        formula_version="general_recovery_v0.1.0",
    )


class _Recovery:
    def __init__(self, hrv_magnitude: float = 85.0, *, rest: bool = False) -> None:
        self._hrv_magnitude = hrv_magnitude
        self._rest = rest

    def recovery_for_day(
        self, *, tenant_id: str, local_health_day: str, target_min: int = 480
    ) -> RecoverySurface:
        if self._rest:
            return _rest_recovery(local_health_day)
        return _hard_recovery(local_health_day, hrv_magnitude=self._hrv_magnitude)


class _Durations:
    """A sleep-duration source with one good night, so /v1/sleep is complete."""

    def daily_sleep_durations(
        self, *, tenant_id: str, local_health_days: list[str]
    ) -> dict[str, float]:
        return dict.fromkeys(local_health_days, 470.0)


class _Load:
    def __init__(self, acwr: float | None) -> None:
        self._acwr = acwr

    def acwr_for_day(self, *, tenant_id: str, local_health_day: str) -> float | None:
        return self._acwr


class _Symptoms:
    def __init__(self, burden: float | None) -> None:
        self._burden = burden

    def symptom_burden_for_day(self, *, tenant_id: str, local_health_day: str) -> float | None:
        return self._burden


def _service(
    acwr: float | None,
    *,
    symptom_burden: float | None = None,
    hrv_magnitude: float = 85.0,
) -> TodaySurfaceService:
    return TodaySurfaceService(
        recovery=_Recovery(hrv_magnitude=hrv_magnitude),
        sleep=SleepSurfaceService(durations=_Durations()),
        load=_Load(acwr),
        symptoms=_Symptoms(symptom_burden),
    )


def _fired_rules(surface: object) -> set[RuleId]:
    recs = {surface.primary_recommendation, *surface.supporting_recommendations}  # type: ignore[attr-defined]
    return {rec.rule_id for rec in recs if rec is not None}


def test_overload_acwr_downshifts_the_hard_label() -> None:
    # ACWR past the 1.3 red band caps a HARD day at MODERATE.
    surface = _service(acwr=1.6).today_for_day(tenant_id="tenant-1", local_health_day=TARGET_DAY)
    assert surface.training_label is TrainingLabel.MODERATE


def test_overload_with_weak_hrv_surfaces_load_ease() -> None:
    # The load_ease rule is conjunctive: over-load AND a weakened HRV component
    # (< 40). With both, the same ACWR that downshifts the label also surfaces
    # the recommendation.
    surface = _service(acwr=1.6, hrv_magnitude=30.0).today_for_day(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    assert RuleId.LOAD_EASE in _fired_rules(surface)


def test_overload_with_strong_hrv_fires_no_load_ease() -> None:
    # Over-load but a healthy HRV component -> load_ease stays dormant; the
    # rule never fires on the ratio alone.
    surface = _service(acwr=1.6, hrv_magnitude=85.0).today_for_day(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    assert RuleId.LOAD_EASE not in _fired_rules(surface)


def test_balanced_acwr_fires_no_load_ease() -> None:
    surface = _service(acwr=1.0, hrv_magnitude=30.0).today_for_day(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    assert RuleId.LOAD_EASE not in _fired_rules(surface)


def test_balanced_acwr_leaves_the_hard_label() -> None:
    # A balanced ratio does not downshift; the base HARD label stands.
    surface = _service(acwr=1.0).today_for_day(tenant_id="tenant-1", local_health_day=TARGET_DAY)
    assert surface.training_label is TrainingLabel.HARD


def test_undefined_acwr_leaves_the_hard_label() -> None:
    # No load coverage -> None -> load rules dormant -> HARD stands.
    surface = _service(acwr=None).today_for_day(tenant_id="tenant-1", local_health_day=TARGET_DAY)
    assert surface.training_label is TrainingLabel.HARD


def test_high_symptom_burden_floors_the_label_at_light() -> None:
    # A recorded symptom burden at/above the 0.75 threshold floors a HARD day
    # at LIGHT via the training-label symptom rule.
    surface = _service(acwr=1.0, symptom_burden=0.9).today_for_day(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    assert surface.training_label is TrainingLabel.LIGHT


def test_low_symptom_burden_leaves_the_hard_label() -> None:
    # A mild burden below the threshold does not downshift.
    surface = _service(acwr=1.0, symptom_burden=0.2).today_for_day(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    assert surface.training_label is TrainingLabel.HARD


def test_absent_symptom_burden_leaves_the_hard_label() -> None:
    # No check-in recorded -> None -> symptom rule dormant -> HARD stands.
    surface = _service(acwr=1.0, symptom_burden=None).today_for_day(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )
    assert surface.training_label is TrainingLabel.HARD


def test_no_load_source_leaves_the_hard_label() -> None:
    # A composite wired without a load source behaves as ACWR-absent.
    service = TodaySurfaceService(
        recovery=_Recovery(),
        sleep=SleepSurfaceService(durations=_Durations()),
    )
    surface = service.today_for_day(tenant_id="tenant-1", local_health_day=TARGET_DAY)
    assert surface.training_label is TrainingLabel.HARD


# --- Suppressed recommendations survive the composite ------------------------


def _rest_service(acwr: float | None) -> TodaySurfaceService:
    return TodaySurfaceService(
        recovery=_Recovery(rest=True),
        sleep=SleepSurfaceService(durations=_Durations()),
        load=_Load(acwr),
    )


def test_suppressed_recommendations_reach_the_surface() -> None:
    """The composite must not drop the losers of conflict resolution.

    ``rest_day`` and ``load_ease`` share the load group, so an over-load day
    with a rest label fires both and suppresses ``load_ease``. Without this the
    field could silently stay empty and nothing else would notice.
    """
    surface = _rest_service(acwr=1.5).today_for_day(
        tenant_id="tenant-1", local_health_day=TARGET_DAY
    )

    assert surface.primary_recommendation is not None
    assert surface.primary_recommendation.rule_id is RuleId.REST_DAY
    suppressed = {rec.rule_id: rec for rec in surface.suppressed_recommendations}
    assert RuleId.LOAD_EASE in suppressed
    # The winner is named, or the client cannot explain the absence.
    assert suppressed[RuleId.LOAD_EASE].suppressed_by is RuleId.REST_DAY


def test_nothing_is_suppressed_when_no_rules_collide() -> None:
    """An empty list is the normal case, not a sign the field is unwired."""
    surface = _service(acwr=1.0).today_for_day(tenant_id="tenant-1", local_health_day=TARGET_DAY)

    assert surface.suppressed_recommendations == ()
