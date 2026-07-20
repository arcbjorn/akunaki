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
from typing import Protocol

from akunaki.application.recovery_inputs import RecoveryInputService
from akunaki.domain.recovery import (
    DEFAULT_SLEEP_TARGET_MIN,
    ComponentCode,
    RecoveryFactor,
    RecoveryGap,
    RecoveryStatus,
    evaluate_recovery,
    recovery_data_gaps,
    recovery_data_gaps_from_codes,
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


@dataclass(frozen=True, slots=True)
class StoredScoreFactor:
    """A factor row read back from storage."""

    factor_code: str
    present: bool
    weight: float | None
    magnitude: float


@dataclass(frozen=True, slots=True)
class StoredRecoveryScore:
    """A persisted recovery score plus its factors, read for serving.

    Carries the stored version and freshness so a served response can disclose
    them; ``available_weight`` is null only for an insufficient row.
    """

    local_health_day: str
    score_code: str
    status: str
    score: int | None
    available_weight: float | None
    confidence: float
    formula_version: str
    freshness_at: str | None
    version_n: int
    factors: tuple[StoredScoreFactor, ...]


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
        components = self._inputs.recovery_components(
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


class StoredScoreReader(Protocol):
    """Port: read the current persisted recovery score for a day."""

    def current_recovery_with_factors(
        self, *, tenant_id: str, local_health_day: str
    ) -> StoredRecoveryScore | None:
        """Return the stored score and its factors, or None when none exists."""
        ...


class RecoveryComputeSource(Protocol):
    """Port: compute the recovery surface for a day (the fallback path)."""

    def recovery_for_day(
        self, *, tenant_id: str, local_health_day: str, target_min: int = ...
    ) -> RecoverySurface:
        """Compute and disclose the day's recovery."""
        ...


class ServedRecoveryService:
    """Serve the recovery surface from storage, computing only as a fallback.

    A persisted score is the authoritative served view: it reflects the last
    recompute and carries a stable version and freshness. A day that has never
    been scored — no recompute has run for it yet — falls back to computing on
    read, so the surface is never empty just because the job has not fired.
    """

    def __init__(
        self,
        *,
        stored: StoredScoreReader,
        compute: RecoveryComputeSource,
    ) -> None:
        self._stored = stored
        self._compute = compute

    def recovery_for_day(
        self,
        *,
        tenant_id: str,
        local_health_day: str,
        target_min: int = DEFAULT_SLEEP_TARGET_MIN,
    ) -> RecoverySurface:
        """Return the stored recovery surface, or compute one if absent."""
        stored = self._stored.current_recovery_with_factors(
            tenant_id=tenant_id, local_health_day=local_health_day
        )
        if stored is None:
            return self._compute.recovery_for_day(
                tenant_id=tenant_id,
                local_health_day=local_health_day,
                target_min=target_min,
            )
        return _surface_from_stored(stored)


def _surface_from_stored(stored: StoredRecoveryScore) -> RecoverySurface:
    """Reconstruct the disclosed surface from a persisted score and its factors.

    Gaps are re-derived from the present factor codes with the same pure rule
    the live evaluation uses, so a served score discloses exactly what it was
    computed with; unrecognized factor codes are ignored for the gate check.
    """
    present_codes = {
        code
        for factor in stored.factors
        if factor.present and (code := _component_code(factor.factor_code)) is not None
    }
    return RecoverySurface(
        local_health_day=stored.local_health_day,
        score_code=stored.score_code,
        status=RecoveryStatus(stored.status),
        score=stored.score,
        confidence=stored.confidence,
        available_weight=stored.available_weight or 0.0,
        factors=tuple(
            RecoveryFactor(
                factor_code=factor.factor_code,
                present=factor.present,
                weight=factor.weight or 0.0,
                magnitude=factor.magnitude,
            )
            for factor in stored.factors
        ),
        data_gaps=recovery_data_gaps_from_codes(present_codes),
        formula_version=stored.formula_version,
    )


def _component_code(raw: str) -> ComponentCode | None:
    """Map a stored factor code back to a ComponentCode, or None if unknown."""
    try:
        return ComponentCode(raw)
    except ValueError:
        return None
