"""Pure OIDC types and ``id_token`` claim validation.

No I/O, no clock of its own: ``now`` is always a parameter, so validation is
deterministic and testable.

Signature verification is deliberately **not** here — it needs JWKS fetched
from the issuer, which is an adapter concern. This module validates the claims
that remain security-critical once a signature has been checked, and refuses
to treat an unverified token as valid.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

# Tolerance for clock skew between us and the issuer, applied to time-based
# claims only. Deliberately small: a large window widens the replay window.
CLOCK_SKEW_SECONDS = 60


class TokenRejection(StrEnum):
    """Why an ``id_token`` was not accepted.

    Callers surface one generic authentication failure; the distinction is for
    server-side metrics and debugging.
    """

    MALFORMED = "malformed"
    ISSUER_MISMATCH = "issuer_mismatch"
    AUDIENCE_MISMATCH = "audience_mismatch"
    EXPIRED = "expired"
    NOT_YET_VALID = "not_yet_valid"
    NONCE_MISMATCH = "nonce_mismatch"
    SUBJECT_MISSING = "subject_missing"


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    """The identity asserted by a validated ``id_token``."""

    issuer: str
    subject: str
    email: str | None

    def __repr__(self) -> str:
        """Redacted: email is sensitive PII, never free log material."""
        return (
            f"VerifiedIdentity(issuer={self.issuer!r}, subject={self.subject!r}, "
            f"email={'<redacted>' if self.email else None})"
        )


@dataclass(frozen=True, slots=True)
class TokenValidation:
    """Result of validating an ``id_token``'s claims."""

    identity: VerifiedIdentity | None = None
    rejection: TokenRejection | None = None

    @property
    def ok(self) -> bool:
        """True when the claims are valid."""
        return self.rejection is None and self.identity is not None


def validate_id_token_claims(
    claims: dict[str, Any],
    *,
    expected_issuer: str,
    expected_audience: str,
    expected_nonce_hash: str,
    hash_nonce: Callable[[str], str],
    now: datetime,
) -> TokenValidation:
    """Validate ``id_token`` claims against the request that produced them.

    Assumes the signature has **already** been verified against the issuer's
    JWKS; this checks the claims that a valid signature alone does not.

    The raw nonce is never stored, so ``expected_nonce_hash`` is compared
    against the hash of the token's own nonce claim. ``nonce`` binds the token
    to *this* authorization request: without it, a token issued for another
    request could be replayed here.
    """
    issuer = claims.get("iss")
    if not isinstance(issuer, str) or issuer != expected_issuer:
        return TokenValidation(rejection=TokenRejection.ISSUER_MISMATCH)

    if not _audience_matches(claims.get("aud"), expected_audience):
        return TokenValidation(rejection=TokenRejection.AUDIENCE_MISMATCH)

    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not _constant_time_equals(
        hash_nonce(nonce), expected_nonce_hash
    ):
        return TokenValidation(rejection=TokenRejection.NONCE_MISMATCH)

    epoch = int(now.timestamp())

    expires_at = claims.get("exp")
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        return TokenValidation(rejection=TokenRejection.MALFORMED)
    if epoch - CLOCK_SKEW_SECONDS >= expires_at:
        return TokenValidation(rejection=TokenRejection.EXPIRED)

    # A token not yet valid, or claiming future issuance, is not trustworthy.
    for claim_name in ("nbf", "iat"):
        value = claims.get(claim_name)
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and epoch + CLOCK_SKEW_SECONDS < value
        ):
            return TokenValidation(rejection=TokenRejection.NOT_YET_VALID)

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        return TokenValidation(rejection=TokenRejection.SUBJECT_MISSING)

    email = claims.get("email")
    return TokenValidation(
        identity=VerifiedIdentity(
            issuer=issuer,
            subject=subject,
            email=email if isinstance(email, str) and email else None,
        )
    )


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
