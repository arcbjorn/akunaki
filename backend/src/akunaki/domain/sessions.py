"""Pure domain types for backend-issued sessions.

No I/O, no crypto imports. The raw token never appears on a stored type — only
:class:`IssuedSession`, which is returned once at issue time and then dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SessionRejection(StrEnum):
    """Why a presented session cookie was not accepted.

    Callers should surface a single generic ``401 unauthenticated`` regardless
    of which value this is; the distinction exists for server-side metrics.
    """

    NOT_FOUND = "not_found"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A newly issued session, including the secrets shown **once**.

    ``token`` goes into the cookie and ``csrf_secret`` to the client; neither
    is ever stored in the clear, so this object must not be persisted or
    logged.
    """

    session_id: str
    user_id: str
    tenant_id: str
    token: str
    csrf_secret: str
    expires_at: str

    def __repr__(self) -> str:
        """Redacted: a session token in a traceback is a live credential."""
        return (
            f"IssuedSession(session_id={self.session_id!r}, "
            f"tenant_id={self.tenant_id!r}, expires_at={self.expires_at!r}, "
            f"token=<redacted>, csrf_secret=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    """A validated session. Carries no secret material."""

    session_id: str
    user_id: str
    tenant_id: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class SessionValidation:
    """Result of validating a presented cookie."""

    session: AuthenticatedSession | None = None
    rejection: SessionRejection | None = None

    @property
    def ok(self) -> bool:
        """True when the session is valid and usable."""
        return self.rejection is None and self.session is not None
