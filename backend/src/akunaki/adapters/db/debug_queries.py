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
    RawPayload,
    RawRevision,
)


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

    @staticmethod
    def _count_where(session: Session, model: object, condition: object) -> int:
        return int(
            session.execute(
                select(func.count()).select_from(model).where(condition)  # type: ignore[arg-type]
            ).scalar_one()
        )
