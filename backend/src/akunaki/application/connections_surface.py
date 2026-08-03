"""The read-only ``/v1/connections`` surface.

Answers *is my data actually flowing?* for the caller's own connections: which
providers are linked, whether each is healthy, when it last synced, and how
much has been ingested.

This replaces the unauthenticated ``/internal/debug/sync-status`` stand-in.
That route took ``tenant_id`` as a **query parameter** — the one thing every
other surface refuses to do — so anyone who could reach it could read any
tenant's connection health. Here the tenant comes from the validated session.

Deliberately **not** health data: statuses, timestamps, and counts only. The
error field carries an error *class*, never a vendor message or body, so a
failing connector cannot leak payload contents into a user-facing surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

__all__ = ["ConnectionStatusSource", "ConnectionSummary", "ConnectionsSurfaceService"]


@dataclass(frozen=True, slots=True)
class ConnectionSummary:
    """One linked connection's status and ingest progress."""

    connection_id: str
    provider: str
    status: str
    last_success_at: str | None
    last_error_class: str | None
    consecutive_failures: int
    transport_pages: int
    raw_revisions: int


class ConnectionStatusSource(Protocol):
    """Port: per-connection status and ingest counts for one tenant."""

    def connection_statuses(self, *, tenant_id: str) -> list[ConnectionSummary]:
        """Every connection the tenant owns, ordered by provider."""
        ...


class ConnectionsSurfaceService:
    """Build the connections view for a tenant."""

    def __init__(self, *, connections: ConnectionStatusSource) -> None:
        self._connections = connections

    def connections_for_tenant(self, *, tenant_id: str) -> list[ConnectionSummary]:
        """Every connection the tenant owns.

        An empty list is a real answer — a user who has linked nothing has no
        connections, which is not an error.
        """
        return self._connections.connection_statuses(tenant_id=tenant_id)
