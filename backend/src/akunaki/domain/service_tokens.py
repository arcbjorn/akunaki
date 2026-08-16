"""Pure domain types for backend-issued service tokens.

A service token is the ``Authorization: Bearer`` credential for a non-browser
caller — an external agent or MCP adapter — over the ``/v1/tools`` surface.
It is tenant- and user-bound like a session, and its **scope** decides whether
it may do anything beyond reading. No I/O, no crypto imports; the raw token
appears only on :class:`IssuedServiceToken`, returned once at issue time.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ServiceTokenScope(StrEnum):
    """What a service token may do.

    Two scopes, and the split is deliberate: the capability to enqueue a sync
    is opt-in **at mint time**, so every token issued before it existed keeps
    exactly the authority it was granted. There is no way to widen a live
    token — the operator mints a new one and revokes the old.
    """

    READ = "read"
    """List and invoke read tools. The default, and what every prior token has."""

    READ_SYNC = "read_sync"
    """Reads, plus tools whose confirmation policy is ``IF_AGENT``.

    Today that is ``connections.sync``: a benign, idempotent, deduplicated
    enqueue of the same job a webhook or the reconcile sweep would queue. It
    deliberately does **not** extend to ``ALWAYS`` tools — a destructive or
    irreversible action stays refused for every service token, whatever its
    scope, because no bearer credential should be able to reach one.
    """

    @property
    def may_invoke_if_agent(self) -> bool:
        """Whether this scope permits an ``IF_AGENT`` tool.

        A property rather than a set comparison at the call site, so adding a
        third scope means editing one place and cannot silently leave a caller
        behind on a stale membership test.
        """
        return self is ServiceTokenScope.READ_SYNC


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
