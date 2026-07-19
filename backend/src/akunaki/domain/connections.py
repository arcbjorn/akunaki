"""Pure domain types for provider connections.

No I/O, no SQLAlchemy, no crypto imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Provider(StrEnum):
    """Providers this platform can link."""

    OURA = "oura"
    GOOGLE_HEALTH = "google_health"
    POLAR = "polar"


class ConnectionStatus(StrEnum):
    """Lifecycle status of one provider connection."""

    PENDING = "pending"
    ACTIVE = "active"
    NEEDS_REAUTH = "needs_reauth"
    REVOKED = "revoked"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class LinkedConnection:
    """A connection row after a successful link or relink."""

    connection_id: str
    tenant_id: str
    provider: Provider
    status: ConnectionStatus
    scopes: tuple[str, ...]
    external_user_id: str | None
