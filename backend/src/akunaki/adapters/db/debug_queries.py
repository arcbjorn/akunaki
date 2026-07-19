"""Read-only queries backing the internal debug surface.

Strictly read-only: nothing here mutates state, so the debug router cannot
become a write path even by accident.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.models import (
    Connection,
    ConnectionHealth,
    FactRecord,
    RawPayload,
    RawRevision,
    SleepSession,
)
from akunaki.domain.sleep_normalizer import ENTITY_TYPE


@dataclass(frozen=True, slots=True)
class ConnectionSyncStatus:
    """Sync progress for one connection."""

    connection_id: str
    provider: str
    status: str
    last_success_at: str | None
    last_error_class: str | None
    consecutive_failures: int
    transport_pages: int
    raw_revisions: int


@dataclass(frozen=True, slots=True)
class LatestSleepFact:
    """The most recent current sleep fact for a tenant."""

    fact_record_id: str
    local_health_day: str | None
    start_utc: str | None
    end_utc: str | None
    duration_min: float
    time_in_bed_min: float | None
    efficiency_pct: float | None
    is_nap: bool
    quality: str
    confidence: float
    normalizer_version: str
    raw_revision_id: str | None
    version_n: int


class DebugQueries:
    """Read-only lookups for the internal debug router."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def sync_status(self, *, tenant_id: str) -> list[ConnectionSyncStatus]:
        """Per-connection sync progress for one tenant."""
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    Connection.id,
                    Connection.provider,
                    Connection.status,
                    ConnectionHealth.last_success_at,
                    ConnectionHealth.last_error_class,
                    ConnectionHealth.consecutive_failures,
                )
                .outerjoin(
                    ConnectionHealth,
                    ConnectionHealth.connection_id == Connection.id,
                )
                .where(Connection.tenant_id == tenant_id)
                .order_by(Connection.provider)
            ).all()

            statuses: list[ConnectionSyncStatus] = []
            for (
                connection_id,
                provider,
                status,
                last_success_at,
                last_error_class,
                failures,
            ) in rows:
                statuses.append(
                    ConnectionSyncStatus(
                        connection_id=connection_id,
                        provider=provider,
                        status=status,
                        last_success_at=last_success_at,
                        last_error_class=last_error_class,
                        consecutive_failures=failures or 0,
                        transport_pages=self._count_where(
                            session, RawPayload, RawPayload.connection_id == connection_id
                        ),
                        raw_revisions=self._count_where(
                            session,
                            RawRevision,
                            RawRevision.tenant_id == tenant_id,
                        ),
                    )
                )
            return statuses

    def latest_sleep_fact(self, *, tenant_id: str) -> LatestSleepFact | None:
        """The most recent current sleep fact, or None when there is none."""
        with self._session_factory() as session:
            row = session.execute(
                select(FactRecord, SleepSession)
                .join(SleepSession, SleepSession.fact_record_id == FactRecord.id)
                .where(
                    FactRecord.tenant_id == tenant_id,
                    FactRecord.entity_type == ENTITY_TYPE,
                    FactRecord.is_current == 1,
                )
                # Most recent night first; local day is the user-facing bucket.
                .order_by(
                    FactRecord.local_health_day.desc(),
                    FactRecord.start_utc.desc(),
                )
                .limit(1)
            ).first()
            if row is None:
                return None

            fact, detail = row
            return LatestSleepFact(
                fact_record_id=fact.id,
                local_health_day=fact.local_health_day,
                start_utc=fact.start_utc,
                end_utc=fact.end_utc,
                duration_min=detail.duration_min,
                time_in_bed_min=detail.time_in_bed_min,
                efficiency_pct=detail.efficiency_pct,
                is_nap=bool(detail.is_nap),
                quality=fact.quality,
                confidence=fact.confidence,
                normalizer_version=fact.normalizer_version,
                raw_revision_id=fact.raw_revision_id,
                version_n=fact.version_n,
            )

    @staticmethod
    def _count_where(session: Session, model: object, condition: object) -> int:
        return int(
            session.execute(
                select(func.count()).select_from(model).where(condition)  # type: ignore[arg-type]
            ).scalar_one()
        )
