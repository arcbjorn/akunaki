"""Webhook signature verification (pure).

No I/O, no clock. Verifies an inbound webhook's HMAC-SHA256 signature over the
exact request body, in **constant time** so a mismatch reveals nothing through
timing. Oura and Polar sign this way (HMAC over the body with a shared secret);
Google Health's rotating public-key scheme is a different verifier and is not
covered here.

The signing secret never leaves the server. A verification result is a plain
boolean: the route decides the HTTP response, and a failed verification is a
generic rejection that discloses no detail about which part failed.
"""

from __future__ import annotations

import hashlib
import hmac

# Providers whose webhooks this module can verify (HMAC-SHA256 over the body).
HMAC_PROVIDERS = frozenset({"oura", "polar"})


def hmac_sha256_hex(*, secret: str, body: bytes) -> str:
    """Return the lowercase hex HMAC-SHA256 of ``body`` under ``secret``."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_hmac_signature(*, secret: str, body: bytes, provided_signature: str) -> bool:
    """Constant-time-verify a provided HMAC-SHA256 hex signature over the body.

    ``provided_signature`` is the vendor-sent hex digest (an optional ``sha256=``
    prefix, used by some providers, is tolerated). Returns False for an empty
    secret or signature rather than raising, so a misconfiguration is a clean
    rejection, never a 500.
    """
    if not secret or not provided_signature:
        return False
    candidate = provided_signature.strip()
    if candidate.lower().startswith("sha256="):
        candidate = candidate[len("sha256=") :]
    expected = hmac_sha256_hex(secret=secret, body=body)
    # compare_digest is constant-time over equal-length inputs and does not leak
    # length beyond what the hex digest already fixes.
    return hmac.compare_digest(expected, candidate.lower())
