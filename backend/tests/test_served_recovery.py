"""Tests for serving the recovery surface from storage.

``ServedRecoveryService`` returns the persisted score when one exists and falls
back to computing only for a day never scored. These use a fake stored reader
and a fake compute service, so the branching logic is tested in isolation.
"""

from __future__ import annotations

from akunaki.application.recovery_surface import (
    RecoverySurface,
    ServedRecoveryService,
    StoredRecoveryScore,
    StoredScoreFactor,
)
from akunaki.domain.recovery import RecoveryGap, RecoveryStatus


class _FakeStored:
    def __init__(self, stored: StoredRecoveryScore | None) -> None:
        self._stored = stored
        self.calls = 0

    def current_recovery_with_factors(
        self, *, tenant_id: str, local_health_day: str
    ) -> StoredRecoveryScore | None:
        self.calls += 1
        return self._stored


class _FakeCompute:
    def __init__(self, surface: RecoverySurface) -> None:
        self._surface = surface
        self.calls = 0

    def recovery_for_day(
        self, *, tenant_id: str, local_health_day: str, target_min: int = 480
    ) -> RecoverySurface:
        self.calls += 1
        return self._surface


def _computed(local_health_day: str = "2026-07-20") -> RecoverySurface:
    return RecoverySurface(
        local_health_day=local_health_day,
        score_code="recovery",
        status=RecoveryStatus.INSUFFICIENT,
        score=None,
        confidence=0.0,
        available_weight=0.20,
        factors=(),
        data_gaps=(RecoveryGap(code="missing_hrv_or_resting_hr"),),
        formula_version="general_recovery_v0.1.0",
    )


def _stored() -> StoredRecoveryScore:
    return StoredRecoveryScore(
        local_health_day="2026-07-20",
        score_code="recovery",
        status="partial",
        score=72,
        available_weight=0.60,
        confidence=0.7,
        formula_version="general_recovery_v0.1.0",
        freshness_at="2026-07-20T12:00:00Z",
        version_n=3,
        factors=(
            StoredScoreFactor(factor_code="hrv", present=True, weight=0.25, magnitude=80.0),
            StoredScoreFactor(
                factor_code="sleep_adherence", present=True, weight=0.20, magnitude=90.0
            ),
            StoredScoreFactor(factor_code="resting_hr", present=True, weight=0.15, magnitude=40.0),
        ),
    )


def test_stored_score_is_served_without_computing() -> None:
    stored = _FakeStored(_stored())
    compute = _FakeCompute(_computed())
    service = ServedRecoveryService(stored=stored, compute=compute)

    surface = service.recovery_for_day(tenant_id="t1", local_health_day="2026-07-20")

    assert surface.score == 72
    assert surface.status is RecoveryStatus.PARTIAL
    assert compute.calls == 0  # never fell back to computing
    # Served metadata flows through from the stored row.
    assert surface.version_n == 3
    assert surface.freshness_at == "2026-07-20T12:00:00Z"


def test_stored_score_reconstructs_gaps_from_present_factors() -> None:
    # The stored factors include HRV/RHR/adherence, all present -> no gate gaps.
    service = ServedRecoveryService(
        stored=_FakeStored(_stored()), compute=_FakeCompute(_computed())
    )
    surface = service.recovery_for_day(tenant_id="t1", local_health_day="2026-07-20")
    assert surface.data_gaps == ()


def test_stored_insufficient_factors_yield_gaps() -> None:
    only_sleep = StoredRecoveryScore(
        local_health_day="2026-07-20",
        score_code="recovery",
        status="insufficient",
        score=None,
        available_weight=None,
        confidence=0.0,
        formula_version="general_recovery_v0.1.0",
        freshness_at="2026-07-20T12:00:00Z",
        version_n=1,
        factors=(
            StoredScoreFactor(
                factor_code="sleep_adherence", present=True, weight=0.20, magnitude=90.0
            ),
            StoredScoreFactor(factor_code="hrv", present=False, weight=0.25, magnitude=0.0),
        ),
    )
    service = ServedRecoveryService(
        stored=_FakeStored(only_sleep), compute=_FakeCompute(_computed())
    )
    surface = service.recovery_for_day(tenant_id="t1", local_health_day="2026-07-20")
    gap_codes = {g.code for g in surface.data_gaps}
    assert "missing_hrv_or_resting_hr" in gap_codes
    assert surface.available_weight == 0.0  # null coverage maps to 0 on read


def test_falls_back_to_compute_when_no_stored_score() -> None:
    stored = _FakeStored(None)
    compute = _FakeCompute(_computed())
    service = ServedRecoveryService(stored=stored, compute=compute)

    surface = service.recovery_for_day(tenant_id="t1", local_health_day="2026-07-20")

    assert compute.calls == 1  # computed on read
    assert surface.status is RecoveryStatus.INSUFFICIENT
