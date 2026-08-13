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

    def existing_connection_id(self, *, tenant_id: str, provider: Provider) -> str | None:
        """Return the id of this tenant's connection for ``provider``, if any.

        Callers seal token material bound to the connection id, but ``link``
        keeps an **existing** row's id on re-consent. Without knowing that id up
        front a re-link seals under an id nothing is stored against, producing
        ciphertext that can never be opened.
        """
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

    def get_connection(self, *, connection_id: str) -> LinkedConnection | None:
        """Return a connection's identity (tenant, provider, status), or None."""
        ...

    def stale_connections(self, *, cutoff: str, limit: int = ...) -> list[tuple[str, str]]:
        """Active connections whose last successful sync predates ``cutoff``."""
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
