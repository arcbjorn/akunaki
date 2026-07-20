"""The read-only ``/v1/recovery`` day surface.

This assembles the recovery components for a tenant's local day, evaluates the
composite, and packages the result with disclosed coverage, signed factors, and
data gaps. It is the last application seam before the HTTP route.

Recovery is the **only** shipping 0-100 score (``general_recovery_v0.1.0``).
For any current tenant the gate fails for want of HRV or RHR, so the surface
returns ``insufficient`` with a null score and an explicit ``data_gaps`` list —
never a fabricated midpoint. When wearable HRV/RHR ingestion lands, the same
path produces a real score with no change here.
"""

from __future__ import annotations

from dataclasses import dataclass

from akunaki.application.recovery_inputs import RecoveryInputService
from akunaki.domain.recovery import (
    DEFAULT_SLEEP_TARGET_MIN,
    RecoveryFactor,
    RecoveryGap,
    RecoveryStatus,
    evaluate_recovery,
    recovery_data_gaps,
)


@dataclass(frozen=True, slots=True)
class RecoverySurface:
    """The recovery view for one local health day."""

    local_health_day: str
    score_code: str
    status: RecoveryStatus
    score: int | None
    confidence: float
    available_weight: float
    factors: tuple[RecoveryFactor, ...]
    data_gaps: tuple[RecoveryGap, ...]
    formula_version: str


class RecoverySurfaceService:
    """Build the recovery surface for a tenant's local day."""

    def __init__(self, *, inputs: RecoveryInputService) -> None:
        self._inputs = inputs

    def recovery_for_day(
        self,
        *,
        tenant_id: str,
        local_health_day: str,
        target_min: int = DEFAULT_SLEEP_TARGET_MIN,
    ) -> RecoverySurface:
        """Assemble, evaluate, and disclose the day's recovery."""
        components = self._inputs.sleep_components(
            tenant_id=tenant_id,
            local_health_day=local_health_day,
            target_min=target_min,
        )
        result = evaluate_recovery(components)
        gaps = recovery_data_gaps(components)
        return RecoverySurface(
            local_health_day=local_health_day,
            score_code="recovery",
            status=result.status,
            score=result.score,
            confidence=result.confidence,
            available_weight=result.available_weight,
            factors=result.factors,
            data_gaps=gaps,
            formula_version=result.formula_version,
        )
