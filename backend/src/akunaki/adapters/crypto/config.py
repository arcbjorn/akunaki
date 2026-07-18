"""Build an :class:`EnvelopeSealer` from application settings.

KEKs are supplied as ``version:base64key`` pairs so they can come from a
platform secret store or KMS export without a bespoke file format. Keys are
never defaulted, generated, or logged: a process that needs sealing and has no
configured KEK fails fast at boot rather than silently writing unprotected or
unreadable data.
"""

from __future__ import annotations

import base64
import binascii

from akunaki.adapters.crypto.envelope import KEY_BYTES, EnvelopeSealer
from akunaki.config import Settings


class SecretConfigError(Exception):
    """KEK configuration is missing or malformed.

    Never carries key material.
    """


def parse_keks(raw: str) -> dict[str, bytes]:
    """Parse ``version:base64key`` pairs into a KEK registry.

    Raises :class:`SecretConfigError` on malformed input, a wrong-length key,
    or a duplicate version. Error messages name the offending *version* only,
    never the key bytes.
    """
    keys: dict[str, bytes] = {}
    for entry in (part.strip() for part in raw.split(",")):
        if not entry:
            continue
        version, separator, encoded = entry.partition(":")
        version = version.strip()
        if not separator or not version or not encoded.strip():
            msg = "each KEK entry must be 'version:base64key'"
            raise SecretConfigError(msg)
        if version in keys:
            msg = f"duplicate KEK version {version!r}"
            raise SecretConfigError(msg)
        try:
            material = base64.b64decode(encoded.strip(), validate=True)
        except (binascii.Error, ValueError) as exc:
            msg = f"KEK {version!r} is not valid base64"
            raise SecretConfigError(msg) from exc
        if len(material) != KEY_BYTES:
            msg = f"KEK {version!r} must decode to exactly {KEY_BYTES} bytes (AES-256)"
            raise SecretConfigError(msg)
        keys[version] = material
    return keys


def build_sealer(settings: Settings) -> EnvelopeSealer:
    """Construct the sealer for this process, or fail fast.

    ``active_kek_version`` may be omitted when exactly one KEK is configured;
    with several, it must be stated explicitly so rotation is never ambiguous.
    """
    keys = parse_keks(settings.secret_keks)
    if not keys:
        msg = (
            "no envelope-encryption KEK configured; set AKUNAKI_SECRET_KEKS "
            "to 'version:base64key' pairs (32 bytes each) before sealing secrets"
        )
        raise SecretConfigError(msg)

    active = settings.active_kek_version.strip()
    if not active:
        if len(keys) > 1:
            msg = "AKUNAKI_ACTIVE_KEK_VERSION is required when multiple KEKs are configured"
            raise SecretConfigError(msg)
        active = next(iter(keys))
    elif active not in keys:
        msg = f"active KEK version {active!r} is not present in AKUNAKI_SECRET_KEKS"
        raise SecretConfigError(msg)

    return EnvelopeSealer(keys=keys, active_key_version=active)
