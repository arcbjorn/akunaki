"""Connection persistence: link, relink, and status transitions.

A link writes the connection row and its sealed secret in **one transaction**,
so a crash can never leave an ``active`` connection with no token material (or
token material with no connection).
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.job_repository import affected_rows
from akunaki.adapters.db.models import Connection, ConnectionHealth, ConnectionSecret
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
            if error_class is not None:
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
