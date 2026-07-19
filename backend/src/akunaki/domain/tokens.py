"""Pure types for OAuth token responses.

No I/O, no HTTP client imports. Token values live here only in transit between
the exchange adapter and the sealing step; they are never persisted in the
clear and never logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from akunaki.domain.jobs import require_aware, to_utc_rfc3339


class TokenExchangeFailure(StrEnum):
    """Why a token exchange or refresh did not yield tokens.

    ``INVALID_GRANT`` is called out separately because it is the signal that a
    connection must be moved to ``needs_reauth`` rather than retried.
    """

    INVALID_GRANT = "invalid_grant"
    INVALID_CLIENT = "invalid_client"
    PROVIDER_ERROR = "provider_error"
    TRANSPORT_ERROR = "transport_error"
    MALFORMED_RESPONSE = "malformed_response"

    @property
    def retryable(self) -> bool:
        """Whether retrying the same request could plausibly succeed.

        A rejected grant or client is a permanent decision by the provider;
        transport and 5xx failures are transient.
        """
        return self in {
            TokenExchangeFailure.PROVIDER_ERROR,
            TokenExchangeFailure.TRANSPORT_ERROR,
        }


@dataclass(frozen=True, slots=True)
class OAuthTokens:
    """Tokens returned by a provider's token endpoint.

    ``expires_at`` is absolute so a stored value stays meaningful after a
    process restart, unlike the provider's relative ``expires_in``.
    """

    access_token: str
    refresh_token: str | None
    expires_at: str | None
    scopes: tuple[str, ...]
    token_type: str

    def __post_init__(self) -> None:
        if not self.access_token:
            msg = "access_token must be non-empty"
            raise ValueError(msg)

    def __repr__(self) -> str:
        """Redacted repr: token values must never reach logs or tracebacks."""
        return (
            f"OAuthTokens(token_type={self.token_type!r}, "
            f"scopes={self.scopes!r}, expires_at={self.expires_at!r}, "
            f"access_token=<redacted>, "
            f"refresh_token={'<redacted>' if self.refresh_token else None})"
        )


@dataclass(frozen=True, slots=True)
class TokenExchangeResult:
    """Outcome of a token exchange or refresh.

    Exactly one of ``tokens`` or ``failure`` is set; ``ok`` distinguishes them.
    """

    tokens: OAuthTokens | None = None
    failure: TokenExchangeFailure | None = None

    @property
    def ok(self) -> bool:
        """True when tokens were obtained."""
        return self.failure is None and self.tokens is not None


def absolute_expiry(now: datetime, expires_in_seconds: int | None) -> str | None:
    """Convert a provider's relative ``expires_in`` to an absolute timestamp.

    Returns None when the provider omits it. Non-positive values yield the
    current instant, meaning "already expired", rather than a time in the past.
    """
    if expires_in_seconds is None:
        return None
    aware = require_aware(now, field_name="now")
    return to_utc_rfc3339(aware + timedelta(seconds=max(expires_in_seconds, 0)))
