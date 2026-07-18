"""Pure domain types for the OAuth authorize/callback handshake.

No I/O, no SQLAlchemy, no crypto imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from akunaki.domain.secrets import SealedSecret


class OAuthStateRejection(StrEnum):
    """Why an authorize state could not be consumed.

    Callers should surface a single generic error to the user regardless of
    which value this is; the distinction exists for server-side metrics and
    debugging, not for the response body.
    """

    NOT_FOUND = "not_found"
    ALREADY_CONSUMED = "already_consumed"
    EXPIRED = "expired"
    REDIRECT_MISMATCH = "redirect_mismatch"


@dataclass(frozen=True, slots=True)
class PendingAuthorization:
    """A persisted authorize attempt awaiting its callback.

    Carries the stored ``state_hash``, never the raw ``state`` — the raw value
    exists only in the redirect handed to the user agent.
    """

    state_id: str
    tenant_id: str
    provider: str
    state_hash: str
    redirect_uri: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class OAuthStateConsumption:
    """Result of attempting to consume an authorize state.

    Exactly one of ``rejection`` or the populated success fields is meaningful:
    ``ok`` distinguishes them.
    """

    state_id: str | None = None
    tenant_id: str | None = None
    provider: str | None = None
    sealed_verifier: SealedSecret | None = None
    redirect_uri: str | None = None
    rejection: OAuthStateRejection | None = None

    @property
    def ok(self) -> bool:
        """True when the state was validly consumed and the verifier released."""
        return self.rejection is None and self.sealed_verifier is not None
