"""Session token and CSRF secret generation and verification.

The raw session token exists only in the cookie sent to the browser. Only its
SHA-256 hash is stored, so a database dump yields no usable session, and
lookup is by hash rather than by comparing secrets.

A plain (unsalted) hash is correct here, unlike for passwords: these tokens
carry 256 bits of entropy from ``secrets.token_bytes``, so they are not
brute-forceable and lookup must be an exact-match index probe.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_TOKEN_ENTROPY_BYTES = 32
_CSRF_ENTROPY_BYTES = 32

# Cookie tokens are prefixed so a leaked string is recognizable in logs or
# secret scanners as a session credential rather than opaque noise.
TOKEN_PREFIX = "aks_"  # noqa: S105 - a marker prefix, not a credential


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def generate_session_token() -> str:
    """Return a fresh opaque session token for the cookie."""
    return TOKEN_PREFIX + _b64url(secrets.token_bytes(_TOKEN_ENTROPY_BYTES))


def generate_csrf_secret() -> str:
    """Return a fresh CSRF secret handed to the client alongside the session."""
    return _b64url(secrets.token_bytes(_CSRF_ENTROPY_BYTES))


def hash_token(token: str) -> str:
    """Return the stored hash of a session token or CSRF secret."""
    if not token:
        msg = "token must be non-empty"
        raise ValueError(msg)
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(token: str, token_hash: str) -> bool:
    """Constant-time check that ``token`` hashes to ``token_hash``."""
    if not token or not token_hash:
        return False
    return hmac.compare_digest(hash_token(token), token_hash)
