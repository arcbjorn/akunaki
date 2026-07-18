"""Secret sealing port: envelope encryption boundary.

Adapters implement this protocol. Domain and ports must not import crypto
libraries, so swapping the local KEK source for an external KMS is an adapter
change only.
"""

from __future__ import annotations

from typing import Protocol

from akunaki.domain.secrets import SealedSecret


class SecretSealerPort(Protocol):
    """Envelope-encrypt and open secret material."""

    def seal(self, plaintext: bytes, *, aad: bytes | None = None) -> SealedSecret:
        """Encrypt ``plaintext`` under a fresh DEK wrapped by the active KEK.

        ``aad`` is optional additional authenticated data bound to the
        ciphertext (not encrypted, but tamper-evident). Callers pass a stable
        context value — such as the owning connection id — so an envelope
        cannot be moved to a different row and still open.
        """
        ...

    def open(self, sealed: SealedSecret, *, aad: bytes | None = None) -> bytes:
        """Decrypt a sealed envelope, or raise ``SecretDecryptionError``.

        The ``aad`` must match the value supplied at seal time.
        """
        ...

    @property
    def active_key_version(self) -> str:
        """KEK version new envelopes are sealed under."""
        ...
