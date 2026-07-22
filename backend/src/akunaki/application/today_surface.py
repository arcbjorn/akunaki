"""The composite ``/v1/today`` day view.

This stitches the shipping blocks — the recovery score, the sleep summary, the
deterministic training label, and the recommendations — into one day view, and
discloses what is missing rather than inventing it. Recovery is the only 0-100
score in v0.1.0. Strain and activity do **not** ship yet, so they are named in
``data_gaps`` — never fabricated.

The composite owns no formula: it delegates to the recovery and sleep surface
services and applies the pure training-label and recommendation rules. The
top-level ``status`` is the recovery status, since recovery is the headline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from akunaki.application.recovery_surface import RecoverySurface
from akunaki.application.sleep_surface import SleepSurfaceService
from akunaki.domain.recommendations import (
    Recommendation,
    RecommendationInputs,
    select_recommendations,
)
from akunaki.domain.recovery import DEFAULT_SLEEP_TARGET_MIN, ComponentCode, RecoveryGap
from akunaki.domain.sleep_summary import SleepSummary
from akunaki.domain.training_label import (
    TrainingInputs,
    TrainingLabel,
    training_label,
)


class RecoverySource(Protocol):
    """Port: produce the recovery surface for a day (stored or computed)."""

    def recovery_for_day(
        self, *, tenant_id: str, local_health_day: str, target_min: int = ...
    ) -> RecoverySurface:
        """Return the recovery surface for the tenant's local day."""
        ...


class AnomalySource(Protocol):
    """Port: whether the tenant has an active high-severity anomaly."""

    def has_active_high_severity(self, *, tenant_id: str) -> bool:
        """True when an active anomaly is high severity (drives the downshift)."""
        ...


# Blocks named in the design's /v1/today body that do not ship in v0.1.0.
# (The training recommendation *does* ship — it is a deterministic label, not a
# blocked score — so it is not listed here.)
_UNSHIPPED_BLOCK_GAPS = (
    RecoveryGap(code="strain_not_available"),
    RecoveryGap(code="activity_not_available"),
)


@dataclass(frozen=True, slots=True)
class TodaySurface:
    """The composite view for one local health day."""

    local_health_day: str
    status: str
    recovery: RecoverySurface
    sleep: SleepSummary | None
    """None when the day has no known sleep at all (disclosed via a data gap)."""
    training_label: TrainingLabel
    ruleset_version: str
    primary_recommendation: Recommendation | None
    supporting_recommendations: tuple[Recommendation, ...]
    data_gaps: tuple[RecoveryGap, ...]
    formula_version: str


class TodaySurfaceService:
    """Assemble the composite day view from the recovery and sleep surfaces."""

    def __init__(
        self,
        *,
        recovery: RecoverySource,
        sleep: SleepSurfaceService,
        anomalies: AnomalySource | None = None,
    ) -> None:
        self._recovery = recovery
        self._sleep = sleep
        self._anomalies = anomalies

    def today_for_day(
        self,
        *,
        tenant_id: str,
        local_health_day: str,
        target_min: int = DEFAULT_SLEEP_TARGET_MIN,
    ) -> TodaySurface:
        """Combine recovery and sleep for the day, disclosing every gap."""
        recovery = self._recovery.recovery_for_day(
            tenant_id=tenant_id,
            local_health_day=local_health_day,
            target_min=target_min,
        )
        sleep = self._sleep.summary_for_day(
            tenant_id=tenant_id,
            local_health_day=local_health_day,
            target_min=target_min,
        )

        # Distinguish "no sleep recorded" from "a real short night": the sleep
        # summary defaults an unknown day to zero duration, which is honest for
        # the /v1/sleep surface but must not read as an actual measurement here.
        sleep_known = _day_has_known_sleep(sleep)
        sleep_block = sleep if sleep_known else None

        gaps: list[RecoveryGap] = []
        if not sleep_known:
            gaps.append(RecoveryGap(code="missing_authoritative_sleep"))
        # Carry the recovery gate's own disclosures through unchanged.
        gaps.extend(recovery.data_gaps)
        gaps.extend(_UNSHIPPED_BLOCK_GAPS)
        deduped_gaps = _dedupe(gaps)

        # Training label and recommendations from the day's disclosed values.
        # A persisted active high-severity anomaly floors the label at light;
        # ACWR has no source, so load rules cannot fire — both honest today.
        high_anomaly = self._anomalies is not None and self._anomalies.has_active_high_severity(
            tenant_id=tenant_id
        )
        hrv_c = _hrv_component_c(recovery)
        label = training_label(
            TrainingInputs(
                recovery_score=recovery.score,
                recovery_status=recovery.status.value,
                confidence=recovery.confidence,
                has_high_severity_anomaly=high_anomaly,
                symptom_burden_n=None,
                severe_symptom_flag=False,
                acwr=None,
                hrv_component_c=hrv_c,
            )
        )
        recs = select_recommendations(
            RecommendationInputs(
                sleep_debt_min=sleep.debt_14d_min if sleep_known else None,
                debt_known_days=sleep.debt_known_days if sleep_known else 0,
                sleep_adherence_pct=sleep.adherence_pct if sleep_known else None,
                acwr=None,
                hrv_component_c=hrv_c,
                training_label_is_rest=label.label is TrainingLabel.REST,
                has_data_gap=bool(deduped_gaps),
            )
        )

        return TodaySurface(
            local_health_day=local_health_day,
            status=recovery.status.value,
            recovery=recovery,
            sleep=sleep_block,
            training_label=label.label,
            ruleset_version=label.ruleset_version,
            primary_recommendation=recs.primary,
            supporting_recommendations=recs.supporting,
            data_gaps=deduped_gaps,
            formula_version=recovery.formula_version,
        )


def _hrv_component_c(recovery: RecoverySurface) -> float | None:
    """The HRV component's 0-100 score from the recovery factors, if present."""
    for factor in recovery.factors:
        if factor.factor_code == ComponentCode.HRV.value and factor.present:
            return factor.magnitude
    return None


def _day_has_known_sleep(sleep: SleepSummary) -> bool:
    """Whether the target day itself has recorded sleep.

    The debt window's ``known_days`` counts every present day in the 14-day
    window, so it cannot answer this. Instead, the target day is known when its
    own duration is positive; a genuine zero-duration night is not a case the
    v0.1.0 data model produces (a recorded session has positive minutes).
    """
    return sleep.duration_min > 0.0


def _dedupe(gaps: list[RecoveryGap]) -> tuple[RecoveryGap, ...]:
    """Preserve order, drop repeats (recovery and the composite can overlap)."""
    seen: set[str] = set()
    unique: list[RecoveryGap] = []
    for gap in gaps:
        if gap.code not in seen:
            seen.add(gap.code)
            unique.append(gap)
    return tuple(unique)
