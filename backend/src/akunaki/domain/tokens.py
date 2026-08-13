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
    # Some providers (Polar) return the vendor's user id alongside the token,
    # which becomes the connection's ``external_user_id``. None when the
    # provider does not disclose one.
    external_user_id: str | None = None

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


class EnrollmentFailure(StrEnum):
    """Why registering a user with a provider's API did not succeed.

    Distinct from ``TokenExchangeFailure``: the grant itself is valid here, but
    the provider will not serve data until the user is enrolled with the
    calling client (Polar AccessLink's ``POST /v3/users``).
    """

    REJECTED = "rejected"
    """Provider refused the enrollment; the grant cannot be used for data."""

    PROVIDER_ERROR = "provider_error"
    TRANSPORT_ERROR = "transport_error"

    @property
    def retryable(self) -> bool:
        """Whether retrying the same enrollment could plausibly succeed."""
        return self in {
            EnrollmentFailure.PROVIDER_ERROR,
            EnrollmentFailure.TRANSPORT_ERROR,
        }


@dataclass(frozen=True, slots=True)
class EnrollmentResult:
    """Outcome of enrolling a user with a provider's API.

    ``already_enrolled`` is a *success*: a re-consent re-registers a user the
    client already has, and treating the provider's conflict response as a
    failure would make re-linking impossible.
    """

    failure: EnrollmentFailure | None = None
    already_enrolled: bool = False

    @property
    def ok(self) -> bool:
        """True when the user is enrolled (newly or already)."""
        return self.failure is None


def absolute_expiry(now: datetime, expires_in_seconds: int | None) -> str | None:
    """Convert a provider's relative ``expires_in`` to an absolute timestamp.

    Returns None when the provider omits it. Non-positive values yield the
    current instant, meaning "already expired", rather than a time in the past.
    """
    if expires_in_seconds is None:
        return None
    aware = require_aware(now, field_name="now")
    return to_utc_rfc3339(aware + timedelta(seconds=max(expires_in_seconds, 0)))
