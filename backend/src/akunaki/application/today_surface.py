"""The composite ``/v1/today`` day view.

This stitches the two shipping blocks — the recovery score and the sleep
summary — into one day view, and discloses what is missing rather than
inventing it. Recovery is the only 0-100 score in v0.1.0; sleep is a
deterministic summary. Strain, activity, and the training recommendation do
**not** ship yet (no load data, no accepted ruleset), so they are absent from
the body and named in ``data_gaps`` — never fabricated.

The composite owns no formula: it delegates to the recovery and sleep surface
services and combines their disclosures. The top-level ``status`` is the
recovery status, since recovery is the day's headline score.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from akunaki.application.recovery_surface import RecoverySurface
from akunaki.application.sleep_surface import SleepSurfaceService
from akunaki.domain.recovery import DEFAULT_SLEEP_TARGET_MIN, RecoveryGap
from akunaki.domain.sleep_summary import SleepSummary


class RecoverySource(Protocol):
    """Port: produce the recovery surface for a day (stored or computed)."""

    def recovery_for_day(
        self, *, tenant_id: str, local_health_day: str, target_min: int = ...
    ) -> RecoverySurface:
        """Return the recovery surface for the tenant's local day."""
        ...


# Blocks named in the design's /v1/today body that do not ship in v0.1.0.
_UNSHIPPED_BLOCK_GAPS = (
    RecoveryGap(code="strain_not_available"),
    RecoveryGap(code="activity_not_available"),
    RecoveryGap(code="training_recommendation_not_available"),
)


@dataclass(frozen=True, slots=True)
class TodaySurface:
    """The composite view for one local health day."""

    local_health_day: str
    status: str
    recovery: RecoverySurface
    sleep: SleepSummary | None
    """None when the day has no known sleep at all (disclosed via a data gap)."""
    data_gaps: tuple[RecoveryGap, ...]
    formula_version: str


class TodaySurfaceService:
    """Assemble the composite day view from the recovery and sleep surfaces."""

    def __init__(
        self,
        *,
        recovery: RecoverySource,
        sleep: SleepSurfaceService,
    ) -> None:
        self._recovery = recovery
        self._sleep = sleep

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

        return TodaySurface(
            local_health_day=local_health_day,
            status=recovery.status.value,
            recovery=recovery,
            sleep=sleep_block,
            data_gaps=_dedupe(gaps),
            formula_version=recovery.formula_version,
        )


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
