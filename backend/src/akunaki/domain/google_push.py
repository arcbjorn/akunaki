"""Google Health push-webhook claim validation (pure).

No I/O, no clock. Google Health delivers webhooks as **Google-signed OIDC
tokens** (the Pub/Sub push authentication model): a Bearer JWT in the request's
``Authorization`` header, signed by Google's rotating JWKS keys. Verifying the
signature (adapter) is not enough — the token's **claims** must also match:

- ``iss`` is Google (``https://accounts.google.com`` or ``accounts.google.com``);
- ``aud`` equals the configured push audience (the endpoint / audience string
  registered with the subscription);
- ``exp`` is in the future (with a small clock skew);
- ``email`` is the expected push **service account**, and ``email_verified`` is
  true — this is the "endpoint authorization" the design calls for, ensuring the
  push came from *our* subscription's identity, not any Google-signed token.

This mirrors the OIDC ``validate_id_token_claims`` split: signature first
(adapter), claim policy here against an injected clock for testable determinism.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

# Small tolerance for clock skew between us and Google, in seconds.
CLOCK_SKEW_SECONDS = 60

# The two issuer spellings Google uses for these tokens.
_GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")


class PushRejection(StrEnum):
    """Why a push token's claims were rejected. A single generic surface reason."""

    MALFORMED = "malformed"
    ISSUER_MISMATCH = "issuer_mismatch"
    AUDIENCE_MISMATCH = "audience_mismatch"
    EXPIRED = "expired"
    EMAIL_MISMATCH = "email_mismatch"
    EMAIL_UNVERIFIED = "email_unverified"


@dataclass(frozen=True, slots=True)
class PushValidation:
    """Result of validating a Google push token's claims."""

    ok: bool
    rejection: PushRejection | None = None


def validate_google_push_claims(
    claims: dict[str, Any],
    *,
    expected_audience: str,
    expected_service_account: str,
    now: datetime,
) -> PushValidation:
    """Validate a Google push OIDC token's claims (signature assumed verified).

    Returns ``ok`` only when the issuer is Google, the audience matches the
    configured endpoint audience, the token has not expired, and the caller
    identity (``email`` + ``email_verified``) is the expected push service
    account. Any failure is a single typed rejection — the route surfaces one
    generic error, never which check failed.
    """
    issuer = claims.get("iss")
    if not isinstance(issuer, str) or issuer not in _GOOGLE_ISSUERS:
        return PushValidation(ok=False, rejection=PushRejection.ISSUER_MISMATCH)

    if not _audience_matches(claims.get("aud"), expected_audience):
        return PushValidation(ok=False, rejection=PushRejection.AUDIENCE_MISMATCH)

    epoch = int(now.timestamp())
    expires_at = claims.get("exp")
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        return PushValidation(ok=False, rejection=PushRejection.MALFORMED)
    if epoch - CLOCK_SKEW_SECONDS >= expires_at:
        return PushValidation(ok=False, rejection=PushRejection.EXPIRED)

    email = claims.get("email")
    if not isinstance(email, str) or not _constant_time_equals(email, expected_service_account):
        return PushValidation(ok=False, rejection=PushRejection.EMAIL_MISMATCH)

    if claims.get("email_verified") is not True:
        return PushValidation(ok=False, rejection=PushRejection.EMAIL_UNVERIFIED)

    return PushValidation(ok=True)


def _audience_matches(audience: object, expected: str) -> bool:
    """``aud`` may be a string or a list of strings, per the JWT spec."""
    if isinstance(audience, str):
        return _constant_time_equals(audience, expected)
    if isinstance(audience, list):
        return any(
            isinstance(entry, str) and _constant_time_equals(entry, expected) for entry in audience
        )
    return False


def _constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)
