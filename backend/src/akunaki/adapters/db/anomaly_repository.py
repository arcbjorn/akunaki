"""Anomaly interval persistence.

An anomaly is an open/closed interval per feature. This repository reads the
current tracked state for a feature and applies a transition: opening a new
interval, updating an open one's severity/clear-run, or closing it. At most one
active anomaly per ``(tenant_id, feature_code)`` (enforced by a partial unique
index), so re-detection continues the interval rather than duplicating it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.models import Anomaly as AnomalyRow
from akunaki.application.anomalies_surface import AnomalyInterval
from akunaki.domain.anomalies import AnomalySeverity, AnomalyState
from akunaki.domain.jobs import require_aware, to_utc_rfc3339


class AnomalyRepository:
    """Read and persist tracked anomaly intervals."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def current_state(self, *, tenant_id: str, feature_code: str) -> AnomalyState | None:
        """Return the active interval's tracked state, or None when none is open."""
        with self._session_factory() as session:
            row = session.execute(
                select(AnomalyRow).where(
                    AnomalyRow.tenant_id == tenant_id,
                    AnomalyRow.feature_code == feature_code,
                    AnomalyRow.is_active == 1,
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return AnomalyState(
                is_open=True,
                severity=AnomalySeverity(row.severity),
                consecutive_clear_days=row.consecutive_clear_days,
                started_on=row.started_on,
                tracked_through=row.tracked_through,
            )

    def last_tracked_day(self, *, tenant_id: str, feature_code: str) -> str | None:
        """The latest local day any of the feature's intervals was advanced by.

        With no interval open this is the day the last one closed, which is
        the frontier an out-of-order recompute must not move anything before.
        """
        with self._session_factory() as session:
            latest = session.execute(
                select(func.max(AnomalyRow.tracked_through)).where(
                    AnomalyRow.tenant_id == tenant_id,
                    AnomalyRow.feature_code == feature_code,
                )
            ).scalar_one_or_none()
            return str(latest) if latest is not None else None

    def open_interval(
        self,
        *,
        anomaly_id: str,
        tenant_id: str,
        feature_code: str,
        severity: AnomalySeverity,
        z_like: float | None,
        formula_version: str,
        local_health_day: str,
        now: datetime,
    ) -> None:
        """Open a new active interval for a feature."""
        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        with self._session_factory() as session, session.begin():
            session.add(
                AnomalyRow(
                    id=anomaly_id,
                    tenant_id=tenant_id,
                    feature_code=feature_code,
                    started_on=local_health_day,
                    ended_on=None,
                    tracked_through=local_health_day,
                    severity=severity.value,
                    z_like=z_like,
                    formula_version=formula_version,
                    is_active=1,
                    consecutive_clear_days=0,
                    created_at=now_s,
                    updated_at=now_s,
                )
            )

    def update_open_interval(
        self,
        *,
        tenant_id: str,
        feature_code: str,
        severity: AnomalySeverity,
        consecutive_clear_days: int,
        local_health_day: str,
        now: datetime,
    ) -> None:
        """Update the active interval's severity, clear-day run, and last day."""
        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        with self._session_factory() as session, session.begin():
            session.execute(
                update(AnomalyRow)
                .where(
                    AnomalyRow.tenant_id == tenant_id,
                    AnomalyRow.feature_code == feature_code,
                    AnomalyRow.is_active == 1,
                )
                .values(
                    severity=severity.value,
                    consecutive_clear_days=consecutive_clear_days,
                    tracked_through=local_health_day,
                    updated_at=now_s,
                )
            )

    def close_interval(
        self,
        *,
        tenant_id: str,
        feature_code: str,
        local_health_day: str,
        now: datetime,
    ) -> None:
        """Close the active interval, ending it on the given day."""
        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        with self._session_factory() as session, session.begin():
            session.execute(
                update(AnomalyRow)
                .where(
                    AnomalyRow.tenant_id == tenant_id,
                    AnomalyRow.feature_code == feature_code,
                    AnomalyRow.is_active == 1,
                )
                .values(
                    is_active=0,
                    ended_on=local_health_day,
                    tracked_through=local_health_day,
                    consecutive_clear_days=0,
                    updated_at=now_s,
                )
            )

    def discard_interval(self, *, tenant_id: str, feature_code: str) -> None:
        """Delete the active interval outright.

        For an interval that opened on a day which, re-read once its facts were
        complete, is not anomalous: it never was one, so closing it would leave
        a zero-length anomaly in the history the user is shown.
        """
        with self._session_factory() as session, session.begin():
            session.execute(
                delete(AnomalyRow).where(
                    AnomalyRow.tenant_id == tenant_id,
                    AnomalyRow.feature_code == feature_code,
                    AnomalyRow.is_active == 1,
                )
            )

    def recent_intervals(
        self, *, tenant_id: str, since_day: str, limit: int = 50
    ) -> list[AnomalyInterval]:
        """Active anomalies plus those that closed on or after ``since_day``.

        "Recent" includes closed intervals on purpose: an anomaly that opened
        Tuesday and cleared Thursday is the part of the story that explains why
        a past day read the way it did. Dropping it the moment it closes would
        leave the user with an unexplained gap.

        Active intervals come first (they are what the user acts on), then the
        most recently ended, then feature code for a stable order.
        """
        with self._session_factory() as session:
            rows = session.execute(
                select(AnomalyRow)
                .where(
                    AnomalyRow.tenant_id == tenant_id,
                    (AnomalyRow.is_active == 1) | (AnomalyRow.ended_on >= since_day),
                )
                .order_by(
                    AnomalyRow.is_active.desc(),
                    AnomalyRow.ended_on.desc(),
                    AnomalyRow.feature_code,
                )
                .limit(limit)
            ).scalars()

            return [
                AnomalyInterval(
                    feature_code=row.feature_code,
                    severity=AnomalySeverity(row.severity),
                    started_on=row.started_on,
                    ended_on=row.ended_on,
                    is_active=bool(row.is_active),
                    formula_version=row.formula_version,
                )
                for row in rows
            ]

    def has_active_high_severity(self, *, tenant_id: str) -> bool:
        """Whether any active anomaly is high severity (for the label downshift)."""
        with self._session_factory() as session:
            row = session.execute(
                select(AnomalyRow.id).where(
                    AnomalyRow.tenant_id == tenant_id,
                    AnomalyRow.is_active == 1,
                    AnomalyRow.severity == AnomalySeverity.HIGH.value,
                )
            ).first()
            return row is not None
