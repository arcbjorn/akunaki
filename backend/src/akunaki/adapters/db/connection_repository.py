"""Connection persistence: link, relink, and status transitions.

A link writes the connection row and its sealed secret in **one transaction**,
so a crash can never leave an ``active`` connection with no token material (or
token material with no connection).
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.job_repository import affected_rows
from akunaki.adapters.db.models import (
    Connection,
    ConnectionHealth,
    ConnectionSecret,
    RawObject,
    RawPayload,
    RawRevision,
)
from akunaki.application.connections_surface import ConnectionSummary
from akunaki.domain.connections import ConnectionStatus, LinkedConnection, Provider
from akunaki.domain.jobs import require_aware, to_utc_rfc3339
from akunaki.domain.secrets import SealedSecret


class ConnectionRepository:
    """Persist provider connections and their envelope-encrypted secrets."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def link(
        self,
        *,
        connection_id: str,
        tenant_id: str,
        provider: Provider,
        sealed_secret: SealedSecret,
        scopes: tuple[str, ...],
        external_user_id: str | None,
        now: datetime,
    ) -> LinkedConnection:
        """Create or refresh a connection and store its sealed tokens atomically.

        Relinking an already-connected provider is normal (re-consent, scope
        change, expired refresh token), so this upserts on the existing
        ``(tenant_id, provider)`` row rather than failing. The supplied
        ``connection_id`` is used only when creating a new row; an existing row
        keeps its identity so foreign keys elsewhere stay valid.
        """
        for name, value in (
            ("connection_id", connection_id),
            ("tenant_id", tenant_id),
        ):
            if not value:
                msg = f"{name} must be non-empty"
                raise ValueError(msg)

        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))
        scopes_json = json.dumps(list(scopes))

        with self._session_factory() as session, session.begin():
            existing = session.execute(
                select(Connection).where(
                    Connection.tenant_id == tenant_id,
                    Connection.provider == provider.value,
                )
            ).scalar_one_or_none()

            if existing is None:
                row = Connection(
                    id=connection_id,
                    tenant_id=tenant_id,
                    provider=provider.value,
                    status=ConnectionStatus.ACTIVE.value,
                    scopes_granted_json=scopes_json,
                    external_user_id=external_user_id,
                    connected_at=now_s,
                    updated_at=now_s,
                )
                session.add(row)
                resolved_id = connection_id
            else:
                existing.status = ConnectionStatus.ACTIVE.value
                existing.scopes_granted_json = scopes_json
                existing.external_user_id = external_user_id
                existing.updated_at = now_s
                resolved_id = existing.id

            # Same transaction: an active connection always has its secret.
            session.merge(
                ConnectionSecret(
                    connection_id=resolved_id,
                    tenant_id=tenant_id,
                    ciphertext=sealed_secret.ciphertext,
                    key_version=sealed_secret.key_version,
                    rotated_at=now_s,
                )
            )
            # Reset failure counters: a fresh link clears prior auth errors.
            session.merge(
                ConnectionHealth(
                    connection_id=resolved_id,
                    tenant_id=tenant_id,
                    last_success_at=now_s,
                    last_error_class=None,
                    consecutive_failures=0,
                )
            )

            return LinkedConnection(
                connection_id=resolved_id,
                tenant_id=tenant_id,
                provider=provider,
                status=ConnectionStatus.ACTIVE,
                scopes=scopes,
                external_user_id=external_user_id,
            )

    def existing_connection_id(self, *, tenant_id: str, provider: Provider) -> str | None:
        """Return the id of this tenant's connection for ``provider``, if any."""
        with self._session_factory() as session:
            return session.execute(
                select(Connection.id).where(
                    Connection.tenant_id == tenant_id,
                    Connection.provider == provider.value,
                )
            ).scalar_one_or_none()

    def mark_status(
        self,
        *,
        connection_id: str,
        status: ConnectionStatus,
        now: datetime,
        error_class: str | None = None,
    ) -> bool:
        """Transition a connection's status. Returns False when unknown.

        Used to flip a connection to ``needs_reauth`` after an ``invalid_grant``
        refusal, or to ``error`` after repeated transient failures.
        """
        if not connection_id:
            msg = "connection_id must be non-empty"
            raise ValueError(msg)
        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))

        with self._session_factory() as session, session.begin():
            result = session.execute(
                update(Connection)
                .where(Connection.id == connection_id)
                .values(status=status.value, updated_at=now_s)
            )
            if affected_rows(result) != 1:
                return False
            # A transition to ACTIVE marks a successful sync; record it (and
            # clear the failure streak) so the reconciliation sweep can tell a
            # fresh connection from a stale one.
            if status is ConnectionStatus.ACTIVE:
                tenant_id = session.execute(
                    select(Connection.tenant_id).where(Connection.id == connection_id)
                ).scalar_one()
                updated = session.execute(
                    update(ConnectionHealth)
                    .where(ConnectionHealth.connection_id == connection_id)
                    .values(last_success_at=now_s, consecutive_failures=0, last_error_class=None)
                )
                if affected_rows(updated) == 0:
                    session.add(
                        ConnectionHealth(
                            connection_id=connection_id,
                            tenant_id=tenant_id,
                            last_success_at=now_s,
                        )
                    )
            elif error_class is not None:
                session.merge(
                    ConnectionHealth(
                        connection_id=connection_id,
                        tenant_id=session.execute(
                            select(Connection.tenant_id).where(Connection.id == connection_id)
                        ).scalar_one(),
                        last_error_class=error_class,
                    )
                )
            return True

    def get_connection(self, *, connection_id: str) -> LinkedConnection | None:
        """Return a connection's identity (tenant, provider, status), or None.

        Used where only the connection id is known — e.g. a webhook ingress that
        must resolve the owning tenant and provider before verifying and
        recording a delivery.
        """
        with self._session_factory() as session:
            row = session.get(Connection, connection_id)
            if row is None:
                return None
            scopes = tuple(json.loads(row.scopes_granted_json))
            return LinkedConnection(
                connection_id=row.id,
                tenant_id=row.tenant_id,
                provider=Provider(row.provider),
                status=ConnectionStatus(row.status),
                scopes=scopes,
                external_user_id=row.external_user_id,
            )

    def stale_connections(self, *, cutoff: str, limit: int = 100) -> list[tuple[str, str]]:
        """Active connections whose last successful sync is older than ``cutoff``.

        Returns ``(connection_id, tenant_id)`` for each ACTIVE connection whose
        ``connection_health.last_success_at`` is null (never synced) or strictly
        older than ``cutoff`` (a UTC RFC3339 instant). Only ACTIVE connections
        are swept — a ``needs_reauth`` or ``error`` connection will not sync
        until the user re-consents, so re-enqueuing it would only burn attempts.
        The stable ``id`` ordering makes the sweep deterministic and pageable.
        """
        with self._session_factory() as session:
            rows = session.execute(
                select(Connection.id, Connection.tenant_id)
                .outerjoin(ConnectionHealth, ConnectionHealth.connection_id == Connection.id)
                .where(
                    Connection.status == ConnectionStatus.ACTIVE.value,
                    (ConnectionHealth.last_success_at.is_(None))
                    | (ConnectionHealth.last_success_at < cutoff),
                )
                .order_by(Connection.id)
                .limit(limit)
            ).all()
        return [(cid, tid) for cid, tid in rows]

    def connection_statuses(self, *, tenant_id: str) -> list[ConnectionSummary]:
        """Per-connection status and ingest counts for one tenant.

        Backs the authenticated ``/v1/connections`` surface. Every count is
        scoped to **its own connection** — a tenant with two providers sees
        each one's real ingest volume, not the tenant-wide total.

        Counts are computed as correlated scalar subqueries in the same
        statement, so the number of round trips does not grow with the number
        of linked connections.
        """
        pages = (
            select(func.count())
            .select_from(RawPayload)
            .where(RawPayload.connection_id == Connection.id)
            .scalar_subquery()
        )
        # ``raw_revisions`` has no direct connection FK; it reaches one through
        # the raw object whose logical record it versions.
        revisions = (
            select(func.count())
            .select_from(RawRevision)
            .join(RawObject, RawObject.id == RawRevision.raw_object_id)
            .where(RawObject.connection_id == Connection.id)
            .scalar_subquery()
        )

        with self._session_factory() as session:
            rows = session.execute(
                select(
                    Connection.id,
                    Connection.provider,
                    Connection.status,
                    ConnectionHealth.last_success_at,
                    ConnectionHealth.last_error_class,
                    ConnectionHealth.consecutive_failures,
                    pages,
                    revisions,
                )
                .outerjoin(ConnectionHealth, ConnectionHealth.connection_id == Connection.id)
                .where(Connection.tenant_id == tenant_id)
                .order_by(Connection.provider, Connection.id)
            ).all()

        return [
            ConnectionSummary(
                connection_id=connection_id,
                provider=provider,
                status=status,
                last_success_at=last_success_at,
                last_error_class=last_error_class,
                consecutive_failures=failures or 0,
                transport_pages=int(transport_pages or 0),
                raw_revisions=int(raw_revisions or 0),
            )
            for (
                connection_id,
                provider,
                status,
                last_success_at,
                last_error_class,
                failures,
                transport_pages,
                raw_revisions,
            ) in rows
        ]

    def get_sealed_secret(self, *, connection_id: str) -> SealedSecret | None:
        """Return the stored sealed tokens for a connection, if any."""
        with self._session_factory() as session:
            row = session.execute(
                select(ConnectionSecret.ciphertext, ConnectionSecret.key_version).where(
                    ConnectionSecret.connection_id == connection_id
                )
            ).one_or_none()
            if row is None:
                return None
            ciphertext, key_version = row
            return SealedSecret(ciphertext=ciphertext, key_version=key_version)

    def revoke(self, *, tenant_id: str, connection_id: str, now: datetime) -> bool:
        """Disconnect: drop the stored secret and mark the connection revoked.

        Both in **one** transaction. A crash between them would otherwise leave
        a connection the user believes is disconnected still holding usable
        vendor tokens, or a revoked row whose secret outlives it.

        Historical facts are deliberately untouched — disconnecting revokes
        credentials, it never destroys history (roadmap decision 2026-07-19).
        Only an explicit privacy delete removes facts.

        Tenant-scoped, so one tenant cannot revoke another's connection; False
        when the connection is unknown **or** not theirs.
        """
        if not connection_id:
            msg = "connection_id must be non-empty"
            raise ValueError(msg)
        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))

        with self._session_factory() as session, session.begin():
            result = session.execute(
                update(Connection)
                .where(
                    Connection.id == connection_id,
                    Connection.tenant_id == tenant_id,
                )
                .values(status=ConnectionStatus.REVOKED.value, updated_at=now_s)
            )
            if affected_rows(result) != 1:
                return False
            # Same transaction: a revoked connection never keeps its tokens.
            session.execute(
                delete(ConnectionSecret).where(ConnectionSecret.connection_id == connection_id)
            )
            return True
