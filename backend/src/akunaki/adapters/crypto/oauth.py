"""OAuth ``state`` hashing and PKCE generation (RFC 7636).

The raw ``state`` value is never persisted: only its SHA-256 hash is stored, so
a leaked database cannot be used to forge a callback. Lookup compares hashes,
and verification uses a constant-time comparison.

PKCE here is **S256** only. ``plain`` is deliberately unsupported: it offers no
protection against a leaked authorization code.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# RFC 7636 allows 43-128 characters for the verifier. 64 random bytes of
# base64url yields 86 characters, comfortably inside that range.
_VERIFIER_ENTROPY_BYTES = 64
_STATE_ENTROPY_BYTES = 32

MIN_VERIFIER_LENGTH = 43
MAX_VERIFIER_LENGTH = 128


def _b64url(raw: bytes) -> str:
    """Base64url-encode without padding, per RFC 7636."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def generate_state() -> str:
    """Return a fresh, unguessable OAuth ``state`` value.

    Returned to the caller for the authorize redirect; only its hash is stored.
    """
    return _b64url(secrets.token_bytes(_STATE_ENTROPY_BYTES))


def generate_nonce() -> str:
    """Return a fresh OIDC ``nonce``.

    Distinct from ``state``: ``state`` protects the redirect against CSRF,
    while ``nonce`` is echoed inside the ``id_token`` and binds that token to
    this specific authorization request.
    """
    return _b64url(secrets.token_bytes(_STATE_ENTROPY_BYTES))


def generate_code_verifier() -> str:
    """Return a fresh PKCE ``code_verifier`` (RFC 7636 length range)."""
    return _b64url(secrets.token_bytes(_VERIFIER_ENTROPY_BYTES))


def code_challenge_s256(code_verifier: str) -> str:
    """Return the S256 ``code_challenge`` for ``code_verifier``.

    This is what goes on the authorize URL; the verifier itself stays sealed
    in the database until the callback.
    """
    _require_valid_verifier(code_verifier)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return _b64url(digest)


def hash_state(state: str) -> str:
    """Return the stored SHA-256 hash of an OAuth ``state`` value.

    A plain hash (no salt) is correct here: ``state`` carries 256 bits of
    entropy, so it is not brute-forceable, and lookup must be by exact hash.
    """
    if not state:
        msg = "state must be non-empty"
        raise ValueError(msg)
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def state_matches(state: str, state_hash: str) -> bool:
    """Constant-time check that ``state`` hashes to ``state_hash``."""
    if not state or not state_hash:
        return False
    return hmac.compare_digest(hash_state(state), state_hash)


def redirect_uri_matches(candidate: str, expected: str) -> bool:
    """Exact-match the callback redirect URI against the stored one.

    Deliberately an exact byte comparison: no normalization, no prefix
    matching, no trailing-slash tolerance. Loose matching here is a known
    source of open-redirect and code-interception bugs.
    """
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate, expected)


def _require_valid_verifier(code_verifier: str) -> None:
    if not (MIN_VERIFIER_LENGTH <= len(code_verifier) <= MAX_VERIFIER_LENGTH):
        msg = (
            f"code_verifier must be {MIN_VERIFIER_LENGTH}-{MAX_VERIFIER_LENGTH} "
            "characters (RFC 7636)"
        )
        raise ValueError(msg)
