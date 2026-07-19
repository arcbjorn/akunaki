"""OIDC ``id_token`` claim validation.

Pure tests: no database, no clock. Each rejection below is a real attack the
claim check exists to stop — a token minted for another client, another issuer,
or another login attempt must never authenticate a session here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from akunaki.domain.oidc import (
    CLOCK_SKEW_SECONDS,
    TokenRejection,
    VerifiedIdentity,
    validate_id_token_claims,
)

NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
ISSUER = "https://auth.example.com"
AUDIENCE = "akunaki-web"
NONCE = "nonce-abc123"


def _identity_hash(value: str) -> str:
    """Deterministic hasher for tests: identity, so nonce == its 'hash'."""
    return value


def _claims(**overrides: Any) -> dict[str, Any]:
    epoch = int(NOW.timestamp())
    values: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "subject-1",
        "nonce": NONCE,
        "exp": epoch + 300,
        "iat": epoch - 5,
        "email": "person@example.com",
    }
    values.update(overrides)
    return {k: v for k, v in values.items() if v is not _ABSENT}


class _Absent:
    """Sentinel for omitting a claim entirely."""


_ABSENT = _Absent()


def _validate(**overrides: Any):  # type: ignore[no-untyped-def]
    return validate_id_token_claims(
        _claims(**overrides),
        expected_issuer=ISSUER,
        expected_audience=AUDIENCE,
        expected_nonce_hash=_identity_hash(NONCE),
        hash_nonce=_identity_hash,
        now=NOW,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_claims_yield_an_identity() -> None:
    result = _validate()

    assert result.ok
    assert result.identity is not None
    assert result.identity.issuer == ISSUER
    assert result.identity.subject == "subject-1"
    assert result.identity.email == "person@example.com"


def test_audience_may_be_a_list() -> None:
    """The JWT spec allows ``aud`` to be an array."""
    result = _validate(aud=["other-client", AUDIENCE])
    assert result.ok


def test_email_is_optional() -> None:
    result = _validate(email=_ABSENT)
    assert result.ok
    assert result.identity is not None
    assert result.identity.email is None


def test_nbf_in_the_past_is_accepted() -> None:
    result = _validate(nbf=int(NOW.timestamp()) - 60)
    assert result.ok


# ---------------------------------------------------------------------------
# Issuer and audience
# ---------------------------------------------------------------------------


def test_wrong_issuer_is_rejected() -> None:
    """A token from another IdP must never authenticate here."""
    result = _validate(iss="https://evil.example.com")
    assert result.rejection is TokenRejection.ISSUER_MISMATCH
    assert result.identity is None


def test_missing_issuer_is_rejected() -> None:
    assert _validate(iss=_ABSENT).rejection is TokenRejection.ISSUER_MISMATCH


def test_wrong_audience_is_rejected() -> None:
    """A token minted for a different client must not be reusable here."""
    result = _validate(aud="some-other-client")
    assert result.rejection is TokenRejection.AUDIENCE_MISMATCH


def test_audience_list_without_us_is_rejected() -> None:
    assert _validate(aud=["a", "b"]).rejection is TokenRejection.AUDIENCE_MISMATCH


def test_non_string_audience_is_rejected() -> None:
    assert _validate(aud=123).rejection is TokenRejection.AUDIENCE_MISMATCH


# ---------------------------------------------------------------------------
# Nonce (replay binding)
# ---------------------------------------------------------------------------


def test_wrong_nonce_is_rejected() -> None:
    """Without this, a token issued for another login could be replayed."""
    result = _validate(nonce="a-different-nonce")
    assert result.rejection is TokenRejection.NONCE_MISMATCH


def test_missing_nonce_is_rejected() -> None:
    assert _validate(nonce=_ABSENT).rejection is TokenRejection.NONCE_MISMATCH


def test_empty_nonce_is_rejected() -> None:
    assert _validate(nonce="").rejection is TokenRejection.NONCE_MISMATCH


# ---------------------------------------------------------------------------
# Time-based claims
# ---------------------------------------------------------------------------


def test_expired_token_is_rejected() -> None:
    result = _validate(exp=int(NOW.timestamp()) - 3600)
    assert result.rejection is TokenRejection.EXPIRED


def test_expiry_within_clock_skew_is_tolerated() -> None:
    """A little skew is normal; a large window would widen replay."""
    result = _validate(exp=int(NOW.timestamp()) - (CLOCK_SKEW_SECONDS - 10))
    assert result.ok


def test_expiry_beyond_clock_skew_is_rejected() -> None:
    result = _validate(exp=int(NOW.timestamp()) - (CLOCK_SKEW_SECONDS + 10))
    assert result.rejection is TokenRejection.EXPIRED


def test_missing_exp_is_malformed() -> None:
    """A token with no expiry must never be treated as valid forever."""
    assert _validate(exp=_ABSENT).rejection is TokenRejection.MALFORMED


def test_non_integer_exp_is_malformed() -> None:
    assert _validate(exp="soon").rejection is TokenRejection.MALFORMED


def test_boolean_exp_is_malformed() -> None:
    # bool is an int subclass in Python; it must not pass as a timestamp.
    assert _validate(exp=True).rejection is TokenRejection.MALFORMED


def test_future_nbf_is_rejected() -> None:
    result = _validate(nbf=int(NOW.timestamp()) + 3600)
    assert result.rejection is TokenRejection.NOT_YET_VALID


def test_future_iat_is_rejected() -> None:
    """A token claiming future issuance is not trustworthy."""
    result = _validate(iat=int(NOW.timestamp()) + 3600)
    assert result.rejection is TokenRejection.NOT_YET_VALID


# ---------------------------------------------------------------------------
# Subject
# ---------------------------------------------------------------------------


def test_missing_subject_is_rejected() -> None:
    """``sub`` is the identity; without it there is nobody to log in."""
    assert _validate(sub=_ABSENT).rejection is TokenRejection.SUBJECT_MISSING


def test_empty_subject_is_rejected() -> None:
    assert _validate(sub="").rejection is TokenRejection.SUBJECT_MISSING


def test_non_string_subject_is_rejected() -> None:
    assert _validate(sub=42).rejection is TokenRejection.SUBJECT_MISSING


# ---------------------------------------------------------------------------
# Leak resistance
# ---------------------------------------------------------------------------


def test_identity_repr_redacts_email() -> None:
    """Email is sensitive PII and must not land in a traceback."""
    identity = VerifiedIdentity(issuer=ISSUER, subject="subject-1", email="person@example.com")
    rendered = repr(identity)

    assert "person@example.com" not in rendered
    assert "<redacted>" in rendered
    assert "subject-1" in rendered


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://evil.example.com"},
        {"aud": "other"},
        {"nonce": "wrong"},
        {"exp": int(NOW.timestamp()) - 9999},
        {"sub": _ABSENT},
    ],
)
def test_no_rejection_returns_an_identity(overrides: dict[str, Any]) -> None:
    """A rejected token must never leak a usable identity."""
    result = validate_id_token_claims(
        _claims(**overrides),
        expected_issuer=ISSUER,
        expected_audience=AUDIENCE,
        expected_nonce_hash=_identity_hash(NONCE),
        hash_nonce=_identity_hash,
        now=NOW,
    )
    assert not result.ok
    assert result.identity is None
