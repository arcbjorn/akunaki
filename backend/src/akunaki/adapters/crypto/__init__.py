"""Crypto adapter: envelope encryption for secret material."""

from akunaki.adapters.crypto.config import SecretConfigError, build_sealer, parse_keks
from akunaki.adapters.crypto.envelope import KEY_BYTES, NONCE_BYTES, EnvelopeSealer
from akunaki.adapters.crypto.oauth import (
    code_challenge_s256,
    generate_code_verifier,
    generate_state,
    hash_state,
    redirect_uri_matches,
    state_matches,
)

__all__ = [
    "KEY_BYTES",
    "NONCE_BYTES",
    "EnvelopeSealer",
    "SecretConfigError",
    "build_sealer",
    "code_challenge_s256",
    "generate_code_verifier",
    "generate_state",
    "hash_state",
    "parse_keks",
    "redirect_uri_matches",
    "state_matches",
]
