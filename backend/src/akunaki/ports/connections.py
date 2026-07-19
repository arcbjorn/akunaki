"""Connection persistence port.

Adapters implement this protocol. Domain and ports must not import SQLAlchemy.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from akunaki.domain.connections import ConnectionStatus, LinkedConnection, Provider
from akunaki.domain.oauth import OAuthStateConsumption, PendingAuthorization
from akunaki.domain.secrets import SealedSecret


class ConnectionRepositoryPort(Protocol):
    """Persist provider connections and their envelope-encrypted secrets."""

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
        """Create or refresh a connection and store its sealed tokens atomically."""
        ...

    def mark_status(
        self,
        *,
        connection_id: str,
        status: ConnectionStatus,
        now: datetime,
        error_class: str | None = None,
    ) -> bool:
        """Transition a connection's status. False when the connection is unknown."""
        ...

    def get_sealed_secret(self, *, connection_id: str) -> SealedSecret | None:
        """Return the stored sealed tokens for a connection, if any."""
        ...


class OAuthStateRepositoryPort(Protocol):
    """Create and atomically consume OAuth authorize state rows."""

    def create(
        self,
        *,
        state_id: str,
        tenant_id: str,
        provider: str,
        state: str,
        sealed_verifier: SealedSecret,
        redirect_uri: str,
        now: datetime,
        ttl: timedelta,
    ) -> PendingAuthorization:
        """Persist one authorize attempt and return its stored identity."""
        ...

    def consume(
        self,
        *,
        state: str,
        redirect_uri: str,
        now: datetime,
    ) -> OAuthStateConsumption:
        """Validate and single-use consume an authorize state."""
        ...
