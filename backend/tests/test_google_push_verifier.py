"""Tests for the Google push verifier (signature + claims).

A real RSA keypair signs the token; a fake JWK client returns the matching
public key, so the full signature + claim path runs without network access.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from akunaki.adapters.connectors.google_push_verifier import GooglePushVerifier

NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
AUD = "https://api.example.com/webhooks/google_health/conn-1"
SA = "push@project.iam.gserviceaccount.com"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _priv_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


class _FakeSigningKey:
    def __init__(self, public_key: Any) -> None:
        self.key = public_key


class _FakeJWKClient:
    """Returns a fixed public key for any token (no network)."""

    def __init__(self, public_key: Any) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token: str) -> _FakeSigningKey:
        return _FakeSigningKey(self._public_key)


def _token(key: rsa.RSAPrivateKey, **overrides: object) -> str:
    claims: dict[str, object] = {
        "iss": "https://accounts.google.com",
        "aud": AUD,
        "exp": int(NOW.timestamp()) + 300,
        "email": SA,
        "email_verified": True,
    }
    claims.update(overrides)
    return jwt.encode(claims, _priv_pem(key), algorithm="RS256")


def _verifier(public_key: Any) -> GooglePushVerifier:
    return GooglePushVerifier(
        expected_audience=AUD,
        expected_service_account=SA,
        jwk_client=_FakeJWKClient(public_key),  # type: ignore[arg-type]
    )


def test_valid_token_verifies() -> None:
    verifier = _verifier(_KEY.public_key())
    assert verifier.verify(bearer_token=_token(_KEY), now=NOW) is True


def test_wrong_signing_key_fails() -> None:
    # Token signed by a different key than the JWK client returns.
    verifier = _verifier(_KEY.public_key())
    assert verifier.verify(bearer_token=_token(_OTHER_KEY), now=NOW) is False


def test_failing_claim_fails_even_with_valid_signature() -> None:
    verifier = _verifier(_KEY.public_key())
    # Correctly signed, but the wrong service account.
    token = _token(_KEY, email="attacker@evil.example.com")
    assert verifier.verify(bearer_token=token, now=NOW) is False


def test_expired_token_fails() -> None:
    verifier = _verifier(_KEY.public_key())
    token = _token(_KEY, exp=int(NOW.timestamp()) - 300)
    assert verifier.verify(bearer_token=token, now=NOW) is False


def test_hs256_downgrade_is_rejected() -> None:
    # An attacker forges an HS256 token; only asymmetric algorithms are allowed.
    forged = jwt.encode(
        {"iss": "https://accounts.google.com", "aud": AUD, "email": SA, "email_verified": True},
        "public-key-masquerading-as-a-shared-secret-32b",
        algorithm="HS256",
    )
    verifier = _verifier(_KEY.public_key())
    assert verifier.verify(bearer_token=forged, now=NOW) is False


def test_empty_token_fails() -> None:
    verifier = _verifier(_KEY.public_key())
    assert verifier.verify(bearer_token="", now=NOW) is False


def test_construction_requires_config() -> None:
    with pytest.raises(ValueError, match="must be non-empty"):
        GooglePushVerifier(expected_audience="", expected_service_account=SA)
    with pytest.raises(ValueError, match="must be non-empty"):
        GooglePushVerifier(expected_audience=AUD, expected_service_account="  ")
