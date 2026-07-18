"""OAuth state hashing and PKCE generation (RFC 7636).

The raw ``state`` must never be derivable from what is stored, PKCE must be
S256, and comparisons must be constant-time.
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from akunaki.adapters.crypto.oauth import (
    MAX_VERIFIER_LENGTH,
    MIN_VERIFIER_LENGTH,
    code_challenge_s256,
    generate_code_verifier,
    generate_state,
    hash_state,
    redirect_uri_matches,
    state_matches,
)


def test_generated_state_is_unique_and_high_entropy() -> None:
    states = {generate_state() for _ in range(256)}
    assert len(states) == 256
    # 32 random bytes base64url-encoded, unpadded.
    assert all(len(s) >= 43 for s in states)


def test_generated_verifier_is_unique_and_rfc_compliant() -> None:
    verifiers = {generate_code_verifier() for _ in range(256)}
    assert len(verifiers) == 256
    for verifier in verifiers:
        assert MIN_VERIFIER_LENGTH <= len(verifier) <= MAX_VERIFIER_LENGTH
        # Unreserved characters only; no padding.
        assert "=" not in verifier
        assert all(c.isalnum() or c in "-_" for c in verifier)


def test_code_challenge_matches_rfc7636_s256() -> None:
    # Independently recompute the transformation from the spec.
    verifier = generate_code_verifier()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode()
        .rstrip("=")
    )
    assert code_challenge_s256(verifier) == expected


def test_code_challenge_is_deterministic_and_differs_per_verifier() -> None:
    a = generate_code_verifier()
    b = generate_code_verifier()
    assert code_challenge_s256(a) == code_challenge_s256(a)
    assert code_challenge_s256(a) != code_challenge_s256(b)


def test_code_challenge_rejects_out_of_range_verifier() -> None:
    with pytest.raises(ValueError, match="RFC 7636"):
        code_challenge_s256("too-short")
    with pytest.raises(ValueError, match="RFC 7636"):
        code_challenge_s256("x" * (MAX_VERIFIER_LENGTH + 1))


def test_state_hash_hides_the_raw_state() -> None:
    state = generate_state()
    hashed = hash_state(state)

    # A leaked database must not reveal a usable state value.
    assert state not in hashed
    assert hashed != state
    assert len(hashed) == 64  # sha256 hex


def test_state_hash_is_deterministic_and_collision_free_across_values() -> None:
    hashes = {hash_state(generate_state()) for _ in range(256)}
    assert len(hashes) == 256
    fixed = generate_state()
    assert hash_state(fixed) == hash_state(fixed)


def test_state_matches_only_for_the_original_value() -> None:
    state = generate_state()
    hashed = hash_state(state)

    assert state_matches(state, hashed) is True
    assert state_matches(generate_state(), hashed) is False
    assert state_matches(state, hash_state(generate_state())) is False


def test_state_matches_rejects_empty_inputs() -> None:
    assert state_matches("", hash_state("x")) is False
    assert state_matches("x", "") is False


def test_hash_state_rejects_empty() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        hash_state("")


def test_redirect_uri_match_is_exact() -> None:
    expected = "https://app.example.com/oauth/oura/callback"
    assert redirect_uri_matches(expected, expected) is True

    # Loose matching here is a known open-redirect / code-interception vector.
    for candidate in (
        expected + "/",
        expected + "?x=1",
        expected.replace("https", "http"),
        expected.replace("app.example.com", "app.example.com.evil.test"),
        expected.upper(),
        " " + expected,
    ):
        assert redirect_uri_matches(candidate, expected) is False, candidate


def test_redirect_uri_match_rejects_empty() -> None:
    assert redirect_uri_matches("", "https://x.test/cb") is False
    assert redirect_uri_matches("https://x.test/cb", "") is False
