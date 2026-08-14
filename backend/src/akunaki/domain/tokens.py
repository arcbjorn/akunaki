"""Pure types for OAuth token responses.

No I/O, no HTTP client imports. Token values live here only in transit between
the exchange adapter and the sealing step; they are never persisted in the
clear and never logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from akunaki.domain.jobs import parse_utc_rfc3339, require_aware, to_utc_rfc3339


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


# How long before expiry a token is treated as due for refresh. A token that
# expires mid-sync would fail the *next* page rather than the first, so the
# window has to cover a whole sync run, not just the moment of the check.
REFRESH_LEEWAY = timedelta(minutes=5)


def needs_refresh(
    expires_at: str | None,
    *,
    now: datetime,
    leeway: timedelta = REFRESH_LEEWAY,
) -> bool:
    """Whether a stored access token should be refreshed before use.

    Refreshing **proactively** on expiry rather than reactively on a 401 means a
    sync never spends a doomed request to discover what the stored expiry
    already said — and, more importantly, an expired grant never flips the
    connection to ``needs_reauth`` for a condition that a refresh would have
    fixed silently.

    A token with **no** stored expiry is treated as still valid: providers that
    issue long-lived tokens (Polar) omit it entirely, and refreshing on every
    sync would burn a grant that never needed one. An unparseable expiry is
    treated as due, since a value we cannot read is not a guarantee.
    """
    if expires_at is None:
        return False
    try:
        expiry = parse_utc_rfc3339(expires_at)
    except ValueError:
        return True
    return require_aware(now, field_name="now") + leeway >= expiry


def merge_refreshed_tokens(stored: dict[str, object], fresh: OAuthTokens) -> dict[str, object]:
    """Merge a refresh response over the stored token set.

    The refresh **token itself** is carried forward when the provider omits it.
    Google returns no ``refresh_token`` on a refresh — that is normal and means
    "keep using the one you have" — so writing the response's ``None`` straight
    through would destroy the only means of ever refreshing again, turning a
    working connection into one needing manual re-consent at the next expiry.
    """
    merged = dict(stored)
    merged["access_token"] = fresh.access_token
    merged["expires_at"] = fresh.expires_at
    merged["token_type"] = fresh.token_type
    if fresh.refresh_token:
        merged["refresh_token"] = fresh.refresh_token
    return merged
