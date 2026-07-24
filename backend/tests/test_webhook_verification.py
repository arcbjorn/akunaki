"""Tests for the pure HMAC-SHA256 webhook signature verification."""

from __future__ import annotations

from akunaki.domain.webhook_verification import (
    HMAC_PROVIDERS,
    hmac_sha256_hex,
    verify_hmac_signature,
)

SECRET = "webhook-signing-SECRET"
BODY = b'{"event":"data.updated","user":"u1"}'


def test_hmac_providers_are_oura_and_polar() -> None:
    assert frozenset({"oura", "polar"}) == HMAC_PROVIDERS


def test_valid_signature_verifies() -> None:
    sig = hmac_sha256_hex(secret=SECRET, body=BODY)
    assert verify_hmac_signature(secret=SECRET, body=BODY, provided_signature=sig)


def test_sha256_prefix_is_tolerated() -> None:
    sig = "sha256=" + hmac_sha256_hex(secret=SECRET, body=BODY)
    assert verify_hmac_signature(secret=SECRET, body=BODY, provided_signature=sig)


def test_uppercase_hex_is_tolerated() -> None:
    sig = hmac_sha256_hex(secret=SECRET, body=BODY).upper()
    assert verify_hmac_signature(secret=SECRET, body=BODY, provided_signature=sig)


def test_wrong_secret_fails() -> None:
    sig = hmac_sha256_hex(secret="other", body=BODY)
    assert not verify_hmac_signature(secret=SECRET, body=BODY, provided_signature=sig)


def test_tampered_body_fails() -> None:
    sig = hmac_sha256_hex(secret=SECRET, body=BODY)
    assert not verify_hmac_signature(
        secret=SECRET, body=b'{"event":"tampered"}', provided_signature=sig
    )


def test_empty_secret_or_signature_fails() -> None:
    sig = hmac_sha256_hex(secret=SECRET, body=BODY)
    assert not verify_hmac_signature(secret="", body=BODY, provided_signature=sig)
    assert not verify_hmac_signature(secret=SECRET, body=BODY, provided_signature="")


def test_garbage_signature_fails() -> None:
    assert not verify_hmac_signature(
        secret=SECRET, body=BODY, provided_signature="not-a-hex-digest"
    )
