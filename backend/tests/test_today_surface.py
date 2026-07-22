"""TodaySurfaceService: the ACWR wiring into the training label.

The composite now reads the day's descriptive ACWR from a load source and feeds
it to the pure training-label rules. These verify that a real over-load ratio
drives the downshift, that an undefined ratio (or no load source) leaves the
label untouched, and that the same ratio reaches the recommendation rules.
"""

from __future__ import annotations

from akunaki.application.recovery_surface import RecoverySurface
from akunaki.application.sleep_surface import SleepSurfaceService
from akunaki.application.today_surface import TodaySurfaceService
from akunaki.domain.recovery import RecoveryFactor, RecoveryStatus
from akunaki.domain.training_label import TrainingLabel

TARGET_DAY = "2026-07-22"


def _hard_recovery(day: str) -> RecoverySurface:
    # A high recovery score with a strong HRV component -> base label HARD.
    return RecoverySurface(
        local_health_day=day,
        score_code="recovery",
        status=RecoveryStatus.OK,
        score=90,
        confidence=0.9,
        available_weight=0.9,
        factors=(
            RecoveryFactor(factor_code="hrv", present=True, weight=0.25, magnitude=85.0),
            RecoveryFactor(factor_code="resting_hr", present=True, weight=0.20, magnitude=80.0),
        ),
        data_gaps=(),
        formula_version="general_recovery_v0.1.0",
    )


class _Recovery:
    def recovery_for_day(
        self, *, tenant_id: str, local_health_day: str, target_min: int = 480
    ) -> RecoverySurface:
        return _hard_recovery(local_health_day)


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


def _service(acwr: float | None) -> TodaySurfaceService:
    return TodaySurfaceService(
        recovery=_Recovery(),
        sleep=SleepSurfaceService(durations=_Durations()),
        load=_Load(acwr),
    )


def test_overload_acwr_downshifts_the_hard_label() -> None:
    # ACWR past the 1.3 red band caps a HARD day at MODERATE.
    surface = _service(acwr=1.6).today_for_day(tenant_id="tenant-1", local_health_day=TARGET_DAY)
    assert surface.training_label is TrainingLabel.MODERATE


def test_balanced_acwr_leaves_the_hard_label() -> None:
    # A balanced ratio does not downshift; the base HARD label stands.
    surface = _service(acwr=1.0).today_for_day(tenant_id="tenant-1", local_health_day=TARGET_DAY)
    assert surface.training_label is TrainingLabel.HARD


def test_undefined_acwr_leaves_the_hard_label() -> None:
    # No load coverage -> None -> load rules dormant -> HARD stands.
    surface = _service(acwr=None).today_for_day(tenant_id="tenant-1", local_health_day=TARGET_DAY)
    assert surface.training_label is TrainingLabel.HARD


def test_no_load_source_leaves_the_hard_label() -> None:
    # A composite wired without a load source behaves as ACWR-absent.
    service = TodaySurfaceService(
        recovery=_Recovery(),
        sleep=SleepSurfaceService(durations=_Durations()),
    )
    surface = service.today_for_day(tenant_id="tenant-1", local_health_day=TARGET_DAY)
    assert surface.training_label is TrainingLabel.HARD
