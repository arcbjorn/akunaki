"""The read-only ``/v1/anomalies`` surface.

Anomalies are deterministic, versioned, **non-diagnostic wellness flags**
(health-engine Stage 4). They say a metric departed far from the user's own
recent baseline — not that anything is wrong medically — so nothing here names
a condition, implies a cause, or advises treatment.

``/v1/today`` already reads anomalies, but only as a single boolean (is any
active one high-severity?) that floors the training label. That is enough to
change a recommendation and not nearly enough to explain it: the user sees a
downshift with no way to learn which signal drove it, how severe it was, or
when it started. This surface discloses the intervals themselves.

Deliberately **not** disclosed: the detector's internal ``z_like`` value. It is
engine bookkeeping against a private baseline, and a bare z-score invites
exactly the over-reading ("my HRV is -3.1!") the non-diagnostic framing exists
to avoid. Severity is the disclosed strength.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from akunaki.domain.anomalies import AnomalySeverity

__all__ = [
    "DEFAULT_RECENT_DAYS",
    "AnomaliesSurfaceService",
    "AnomalyInterval",
    "AnomalyIntervalSource",
]

# How far back a closed anomaly stays "recent". Matches the 14-day window the
# sleep-debt surface discloses, so the product speaks in one span of days.
DEFAULT_RECENT_DAYS = 14


@dataclass(frozen=True, slots=True)
class AnomalyInterval:
    """One tracked anomaly interval, as disclosed to the caller."""

    feature_code: str
    severity: AnomalySeverity
    started_on: str
    ended_on: str | None
    is_active: bool
    formula_version: str


class AnomalyIntervalSource(Protocol):
    """Port: active and recently-closed anomaly intervals for a tenant."""

    def recent_intervals(
        self, *, tenant_id: str, since_day: str, limit: int = ...
    ) -> list[AnomalyInterval]:
        """Active intervals plus those that ended on or after ``since_day``."""
        ...


class AnomaliesSurfaceService:
    """Build the anomalies view for a tenant."""

    def __init__(self, *, anomalies: AnomalyIntervalSource) -> None:
        self._anomalies = anomalies

    def anomalies_for_tenant(self, *, tenant_id: str, since_day: str) -> list[AnomalyInterval]:
        """Active and recently-closed anomalies.

        An empty list is a real answer — no flagged signals is the healthy,
        common case, not an error or a gap in the data.
        """
        return self._anomalies.recent_intervals(tenant_id=tenant_id, since_day=since_day)
