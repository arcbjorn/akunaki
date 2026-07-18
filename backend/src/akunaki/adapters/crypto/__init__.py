"""Crypto adapter: envelope encryption for secret material."""

from akunaki.adapters.crypto.config import SecretConfigError, build_sealer, parse_keks
from akunaki.adapters.crypto.envelope import KEY_BYTES, NONCE_BYTES, EnvelopeSealer

__all__ = [
    "KEY_BYTES",
    "NONCE_BYTES",
    "EnvelopeSealer",
    "SecretConfigError",
    "build_sealer",
    "parse_keks",
]
