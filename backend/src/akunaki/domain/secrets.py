"""Pure types for envelope-encrypted secret material.

No I/O and no crypto imports: this module only describes the shape of a
sealed secret so domain and ports stay implementation-free.

Envelope shape: a fresh random **DEK** encrypts the plaintext, and the DEK is
itself encrypted by a **KEK** identified by ``key_version``. Rotating a KEK
therefore re-wraps DEKs without touching plaintext, and ``key_version`` records
which KEK a given envelope needs.
"""

from __future__ import annotations

from dataclasses import dataclass

# Serialized envelope format identifier. Bumped only for a breaking layout
# change so old ciphertext stays decryptable by version dispatch.
ENVELOPE_FORMAT_V1 = 1


@dataclass(frozen=True, slots=True)
class SealedSecret:
    """One envelope-encrypted value plus the KEK version needed to open it.

    ``ciphertext`` is the fully serialized envelope (wrapped DEK, nonces, and
    the encrypted payload). It is opaque to callers and safe to persist as a
    BLOB; it never contains plaintext.
    """

    ciphertext: bytes
    key_version: str

    def __post_init__(self) -> None:
        if not self.ciphertext:
            msg = "ciphertext must be non-empty"
            raise ValueError(msg)
        if not self.key_version.strip():
            msg = "key_version must be a non-empty string"
            raise ValueError(msg)

    def __repr__(self) -> str:
        """Redacted repr: never leak ciphertext bytes into logs or tracebacks."""
        return (
            f"SealedSecret(key_version={self.key_version!r}, "
            f"ciphertext=<{len(self.ciphertext)} bytes>)"
        )


class SecretDecryptionError(Exception):
    """Envelope could not be opened (wrong key, unknown version, or tampering).

    Deliberately carries no plaintext, key material, or ciphertext detail.
    """
