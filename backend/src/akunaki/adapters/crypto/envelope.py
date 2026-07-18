"""AES-256-GCM envelope encryption over a versioned KEK registry.

Scheme (format v1):

1. Generate a fresh random 256-bit **DEK** per ``seal`` call.
2. Encrypt the plaintext with AES-GCM under the DEK, with a fresh 96-bit nonce
   and the caller's AAD bound in.
3. Wrap (encrypt) the DEK with AES-GCM under the active **KEK**, with its own
   fresh nonce, binding the ``key_version`` as AAD so a wrapped DEK cannot be
   replayed under a different version label.
4. Serialize everything into one opaque ciphertext blob.

Nonces are never reused: every seal draws fresh random values, and a DEK is
used for exactly one message.  KEKs come from a caller-supplied registry so
production can source them from a KMS or secret manager while local and test
runs use in-process keys.
"""

from __future__ import annotations

import os
import struct
from collections.abc import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from akunaki.domain.secrets import ENVELOPE_FORMAT_V1, SealedSecret, SecretDecryptionError

KEY_BYTES = 32  # AES-256
NONCE_BYTES = 12  # GCM standard nonce size
_MAX_KEY_VERSION_BYTES = 255

# Envelope layout (all big-endian):
#   uint8   format
#   uint8   key_version length
#   bytes   key_version (utf-8)
#   12B     KEK nonce
#   uint16  wrapped DEK length
#   bytes   wrapped DEK
#   12B     DEK nonce
#   bytes   payload ciphertext (GCM tag appended by AESGCM)
_HEADER = struct.Struct(">BB")


class EnvelopeSealer:
    """Envelope encryption over a versioned KEK registry.

    ``keys`` maps ``key_version`` to a 32-byte KEK. Old versions must remain
    present so existing ciphertext stays readable after rotation; new envelopes
    are always sealed under ``active_key_version``.
    """

    def __init__(self, *, keys: Mapping[str, bytes], active_key_version: str) -> None:
        if not keys:
            msg = "at least one KEK is required"
            raise ValueError(msg)
        for version, key in keys.items():
            if not version.strip():
                msg = "key_version must be a non-empty string"
                raise ValueError(msg)
            if len(version.encode("utf-8")) > _MAX_KEY_VERSION_BYTES:
                msg = f"key_version {version!r} is too long to serialize"
                raise ValueError(msg)
            if len(key) != KEY_BYTES:
                msg = f"KEK {version!r} must be exactly {KEY_BYTES} bytes (AES-256)"
                raise ValueError(msg)
        if active_key_version not in keys:
            msg = f"active_key_version {active_key_version!r} is not present in keys"
            raise ValueError(msg)

        self._keys = dict(keys)
        self._active = active_key_version

    @property
    def active_key_version(self) -> str:
        """KEK version new envelopes are sealed under."""
        return self._active

    def seal(self, plaintext: bytes, *, aad: bytes | None = None) -> SealedSecret:
        """Encrypt ``plaintext`` under a fresh DEK wrapped by the active KEK."""
        version = self._active
        kek = AESGCM(self._keys[version])

        dek_bytes = os.urandom(KEY_BYTES)
        dek_nonce = os.urandom(NONCE_BYTES)
        payload = AESGCM(dek_bytes).encrypt(dek_nonce, plaintext, aad)

        # Bind the version label into the wrap so a wrapped DEK cannot be
        # relabelled as belonging to a different KEK version.
        kek_nonce = os.urandom(NONCE_BYTES)
        wrapped_dek = kek.encrypt(kek_nonce, dek_bytes, version.encode("utf-8"))

        version_bytes = version.encode("utf-8")
        ciphertext = b"".join(
            (
                _HEADER.pack(ENVELOPE_FORMAT_V1, len(version_bytes)),
                version_bytes,
                kek_nonce,
                struct.pack(">H", len(wrapped_dek)),
                wrapped_dek,
                dek_nonce,
                payload,
            )
        )
        return SealedSecret(ciphertext=ciphertext, key_version=version)

    def open(self, sealed: SealedSecret, *, aad: bytes | None = None) -> bytes:
        """Decrypt a sealed envelope.

        Raises :class:`SecretDecryptionError` for an unknown key version,
        malformed envelope, wrong AAD, or any authentication failure. The
        error never carries plaintext or key material.
        """
        try:
            version, kek_nonce, wrapped_dek, dek_nonce, payload = _parse(sealed.ciphertext)
        except (struct.error, IndexError, UnicodeDecodeError, ValueError) as exc:
            raise SecretDecryptionError("malformed envelope") from exc

        if version != sealed.key_version:
            # The column and the envelope disagree about which KEK applies.
            raise SecretDecryptionError("key version mismatch")

        kek_material = self._keys.get(version)
        if kek_material is None:
            raise SecretDecryptionError("unknown key version")

        try:
            dek_bytes = AESGCM(kek_material).decrypt(
                kek_nonce,
                wrapped_dek,
                version.encode("utf-8"),
            )
            return AESGCM(dek_bytes).decrypt(dek_nonce, payload, aad)
        except InvalidTag as exc:
            raise SecretDecryptionError("authentication failed") from exc


def _parse(blob: bytes) -> tuple[str, bytes, bytes, bytes, bytes]:
    """Split a serialized envelope into its parts (format v1 only)."""
    fmt, version_len = _HEADER.unpack_from(blob, 0)
    if fmt != ENVELOPE_FORMAT_V1:
        msg = f"unsupported envelope format {fmt}"
        raise ValueError(msg)

    offset = _HEADER.size
    version = blob[offset : offset + version_len].decode("utf-8")
    if len(version) == 0:
        msg = "empty key version"
        raise ValueError(msg)
    offset += version_len

    kek_nonce = blob[offset : offset + NONCE_BYTES]
    offset += NONCE_BYTES

    (wrapped_len,) = struct.unpack_from(">H", blob, offset)
    offset += 2
    wrapped_dek = blob[offset : offset + wrapped_len]
    offset += wrapped_len

    dek_nonce = blob[offset : offset + NONCE_BYTES]
    offset += NONCE_BYTES

    payload = blob[offset:]
    if (
        len(kek_nonce) != NONCE_BYTES
        or len(dek_nonce) != NONCE_BYTES
        or len(wrapped_dek) != wrapped_len
        or not payload
    ):
        msg = "truncated envelope"
        raise ValueError(msg)

    return version, kek_nonce, wrapped_dek, dek_nonce, payload
