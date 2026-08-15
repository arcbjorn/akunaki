"""Pure domain types for backend-issued service tokens.

A service token is the ``Authorization: Bearer`` credential for a non-browser
caller — an external agent or MCP adapter — over the ``/v1/tools`` surface.
It is tenant- and user-bound like a session, but read-scoped: it can list and
invoke read tools, never mutate. No I/O, no crypto imports; the raw token
appears only on :class:`IssuedServiceToken`, returned once at issue time.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ServiceTokenScope(StrEnum):
    """What a service token may do. Only reads exist today."""

    READ = "read"


class ServiceTokenRejection(StrEnum):
    """Why a presented bearer token was not accepted.

    Callers surface one generic ``401 unauthenticated`` regardless of value;
    the distinction exists for server-side metrics only.
    """

    NOT_FOUND = "not_found"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class IssuedServiceToken:
    """A newly issued service token, including the secret shown **once**.

    ``token`` goes to the operator minting it; only its hash is stored, so
    this object must not be persisted or logged.
    """

    token_id: str
    tenant_id: str
    user_id: str
    name: str
    scope: ServiceTokenScope
    token: str
    expires_at: str | None

    def __repr__(self) -> str:
        """Redacted: a service token in a traceback is a live credential."""
        return (
            f"IssuedServiceToken(token_id={self.token_id!r}, "
            f"tenant_id={self.tenant_id!r}, name={self.name!r}, "
            f"scope={self.scope!r}, expires_at={self.expires_at!r}, "
            f"token=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class AuthenticatedServiceToken:
    """A validated service token. Carries no secret material."""

    token_id: str
    tenant_id: str
    user_id: str
    scope: ServiceTokenScope
    expires_at: str | None


@dataclass(frozen=True, slots=True)
class ServiceTokenValidation:
    """Result of validating a presented bearer token."""

    principal: AuthenticatedServiceToken | None = None
    rejection: ServiceTokenRejection | None = None

    @property
    def ok(self) -> bool:
        """True when the token is valid and usable."""
        return self.rejection is None and self.principal is not None
