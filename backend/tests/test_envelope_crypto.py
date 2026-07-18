"""Envelope encryption: correctness, tamper-evidence, and rotation.

These are security-critical invariants. Every failure mode below (wrong key,
wrong AAD, flipped bit, replayed envelope) must surface as
``SecretDecryptionError`` rather than returning wrong or partial plaintext.
"""

from __future__ import annotations

import os

import pytest

from akunaki.adapters.crypto.envelope import KEY_BYTES, NONCE_BYTES, EnvelopeSealer
from akunaki.domain.secrets import ENVELOPE_FORMAT_V1, SealedSecret, SecretDecryptionError

KEK_V1 = b"\x01" * KEY_BYTES
KEK_V2 = b"\x02" * KEY_BYTES
TOKEN = b'{"refresh_token":"rt-abc123","access_token":"at-xyz789"}'


def _sealer(active: str = "v1") -> EnvelopeSealer:
    return EnvelopeSealer(keys={"v1": KEK_V1, "v2": KEK_V2}, active_key_version=active)


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_seal_open_roundtrip() -> None:
    sealer = _sealer()
    sealed = sealer.seal(TOKEN)

    assert sealer.open(sealed) == TOKEN
    assert sealed.key_version == "v1"


def test_ciphertext_does_not_contain_plaintext() -> None:
    sealer = _sealer()
    sealed = sealer.seal(TOKEN)

    # The most basic guarantee: no readable secret survives in the blob.
    assert TOKEN not in sealed.ciphertext
    assert b"rt-abc123" not in sealed.ciphertext
    assert b"refresh_token" not in sealed.ciphertext


def _payload_of(ciphertext: bytes) -> bytes:
    """Return the DEK-encrypted payload segment of a v1 envelope.

    Asserting on the whole blob is not enough: a random KEK nonce in the
    header would mask a reused DEK/nonce in the payload, which is exactly the
    catastrophic case (identical plaintexts producing identical payloads).
    """
    version_len = ciphertext[1]
    offset = 2 + version_len + NONCE_BYTES
    wrapped_len = int.from_bytes(ciphertext[offset : offset + 2], "big")
    return ciphertext[offset + 2 + wrapped_len + NONCE_BYTES :]


def _unwrap_dek(ciphertext: bytes, kek: bytes) -> bytes:
    """Decrypt the wrapped DEK out of a v1 envelope, for randomness assertions."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    version_len = ciphertext[1]
    version = ciphertext[2 : 2 + version_len]
    return AESGCM(kek).decrypt(
        _kek_nonce_of(ciphertext),
        _wrapped_dek_of(ciphertext),
        version,
    )


def _kek_nonce_of(ciphertext: bytes) -> bytes:
    """Return the KEK nonce of a v1 envelope."""
    version_len = ciphertext[1]
    start = 2 + version_len
    return ciphertext[start : start + NONCE_BYTES]


def _wrapped_dek_of(ciphertext: bytes) -> bytes:
    """Return the KEK-wrapped DEK segment of a v1 envelope."""
    version_len = ciphertext[1]
    offset = 2 + version_len + NONCE_BYTES
    wrapped_len = int.from_bytes(ciphertext[offset : offset + 2], "big")
    return ciphertext[offset + 2 : offset + 2 + wrapped_len]


def _dek_nonce_of(ciphertext: bytes) -> bytes:
    """Return the DEK nonce of a v1 envelope."""
    version_len = ciphertext[1]
    offset = 2 + version_len + NONCE_BYTES
    wrapped_len = int.from_bytes(ciphertext[offset : offset + 2], "big")
    start = offset + 2 + wrapped_len
    return ciphertext[start : start + NONCE_BYTES]


def test_same_plaintext_seals_to_different_ciphertexts() -> None:
    # Fresh DEK and nonces per call: identical inputs must not be linkable.
    sealer = _sealer()
    first = sealer.seal(TOKEN)
    second = sealer.seal(TOKEN)

    assert first.ciphertext != second.ciphertext
    assert sealer.open(first) == sealer.open(second) == TOKEN


def test_identical_plaintexts_have_distinct_payloads_and_nonces() -> None:
    """No plaintext-equality oracle: the payload itself must differ.

    A random KEK nonce alone would make whole-blob comparison pass while
    identical plaintexts still encrypted to identical payload bytes, so this
    asserts on the DEK-encrypted segment and its nonce directly.
    """
    sealer = _sealer()
    seals = [sealer.seal(TOKEN) for _ in range(8)]

    payloads = {_payload_of(s.ciphertext) for s in seals}
    nonces = {_dek_nonce_of(s.ciphertext) for s in seals}

    assert len(payloads) == len(seals), "identical plaintexts produced repeated payloads"
    # GCM nonce reuse under one key is catastrophic; never reuse across seals.
    assert len(nonces) == len(seals), "DEK nonce was reused across seals"


def test_each_seal_uses_a_fresh_dek() -> None:
    """A per-message DEK bounds the blast radius of a single key leak.

    Asserting the *wrapped* bytes differ would prove nothing (a random KEK
    nonce alone makes them differ even with a constant DEK), so this unwraps
    each envelope and compares the actual DEKs.
    """
    sealer = _sealer()
    seals = [sealer.seal(b"same") for _ in range(8)]

    deks = {_unwrap_dek(s.ciphertext, KEK_V1) for s in seals}
    assert len(deks) == len(seals), "the same DEK was reused across seals"


def test_kek_nonce_is_fresh_per_seal() -> None:
    """The KEK nonce must vary too: reuse under one KEK is also GCM misuse."""
    sealer = _sealer()
    seals = [sealer.seal(b"same") for _ in range(8)]

    kek_nonces = {_kek_nonce_of(s.ciphertext) for s in seals}
    assert len(kek_nonces) == len(seals), "KEK nonce was reused across seals"


def test_empty_and_large_payloads_roundtrip() -> None:
    sealer = _sealer()
    # Empty plaintext still produces a non-empty authenticated envelope.
    empty = sealer.seal(b"")
    assert sealer.open(empty) == b""

    large = os.urandom(256 * 1024)
    assert sealer.open(sealer.seal(large)) == large


def test_binary_payload_roundtrips_exactly() -> None:
    sealer = _sealer()
    blob = bytes(range(256))
    assert sealer.open(sealer.seal(blob)) == blob


# ---------------------------------------------------------------------------
# Additional authenticated data
# ---------------------------------------------------------------------------


def test_aad_roundtrip() -> None:
    sealer = _sealer()
    sealed = sealer.seal(TOKEN, aad=b"conn-1")
    assert sealer.open(sealed, aad=b"conn-1") == TOKEN


def test_wrong_aad_fails() -> None:
    # An envelope must not open under a different row's context, which is what
    # stops a stolen ciphertext being replayed onto another connection.
    sealer = _sealer()
    sealed = sealer.seal(TOKEN, aad=b"conn-1")

    with pytest.raises(SecretDecryptionError):
        sealer.open(sealed, aad=b"conn-2")


def test_missing_aad_fails_when_sealed_with_aad() -> None:
    sealer = _sealer()
    sealed = sealer.seal(TOKEN, aad=b"conn-1")

    with pytest.raises(SecretDecryptionError):
        sealer.open(sealed)


def test_unexpected_aad_fails_when_sealed_without() -> None:
    sealer = _sealer()
    sealed = sealer.seal(TOKEN)

    with pytest.raises(SecretDecryptionError):
        sealer.open(sealed, aad=b"conn-1")


# ---------------------------------------------------------------------------
# Tamper evidence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("index", [0, 1, 5, 20, 45, -1])
def test_flipped_bit_anywhere_fails(index: int) -> None:
    sealer = _sealer()
    sealed = sealer.seal(TOKEN)
    corrupted = bytearray(sealed.ciphertext)
    corrupted[index] ^= 0x01

    with pytest.raises(SecretDecryptionError):
        sealer.open(SealedSecret(ciphertext=bytes(corrupted), key_version=sealed.key_version))


def test_truncated_ciphertext_fails() -> None:
    sealer = _sealer()
    sealed = sealer.seal(TOKEN)

    with pytest.raises(SecretDecryptionError):
        sealer.open(SealedSecret(ciphertext=sealed.ciphertext[:-4], key_version=sealed.key_version))


def test_garbage_ciphertext_fails() -> None:
    sealer = _sealer()
    with pytest.raises(SecretDecryptionError):
        sealer.open(SealedSecret(ciphertext=b"not an envelope at all", key_version="v1"))


def test_unsupported_format_byte_fails() -> None:
    sealer = _sealer()
    sealed = sealer.seal(TOKEN)
    bumped = bytearray(sealed.ciphertext)
    bumped[0] = ENVELOPE_FORMAT_V1 + 9

    with pytest.raises(SecretDecryptionError):
        sealer.open(SealedSecret(ciphertext=bytes(bumped), key_version=sealed.key_version))


def test_key_version_label_mismatch_fails() -> None:
    # The column's key_version must agree with the envelope's own label.
    sealer = _sealer()
    sealed = sealer.seal(TOKEN)

    with pytest.raises(SecretDecryptionError, match="key version mismatch"):
        sealer.open(SealedSecret(ciphertext=sealed.ciphertext, key_version="v2"))


def test_wrapped_dek_cannot_be_swapped_between_envelopes() -> None:
    """Splicing another envelope's wrapped DEK must not yield plaintext."""
    sealer = _sealer()
    a = sealer.seal(b"secret-a")
    b = sealer.seal(b"secret-b")

    # Same layout and versions, so the splice is byte-compatible by
    # construction; only authentication should stop it.
    header_len = 2 + len(b"v1") + NONCE_BYTES + 2
    wrapped_len = 32 + 16  # DEK + GCM tag
    spliced = bytearray(a.ciphertext)
    spliced[header_len : header_len + wrapped_len] = b.ciphertext[
        header_len : header_len + wrapped_len
    ]

    with pytest.raises(SecretDecryptionError):
        sealer.open(SealedSecret(ciphertext=bytes(spliced), key_version="v1"))


# ---------------------------------------------------------------------------
# Key hierarchy and rotation
# ---------------------------------------------------------------------------


def test_wrong_kek_cannot_open() -> None:
    sealed = _sealer().seal(TOKEN)
    other = EnvelopeSealer(keys={"v1": os.urandom(KEY_BYTES)}, active_key_version="v1")

    with pytest.raises(SecretDecryptionError):
        other.open(sealed)


def test_unknown_key_version_reports_cleanly() -> None:
    sealed = _sealer(active="v2").seal(TOKEN)
    # A sealer that never learned about v2 (e.g. stale KMS view).
    limited = EnvelopeSealer(keys={"v1": KEK_V1}, active_key_version="v1")

    with pytest.raises(SecretDecryptionError, match="unknown key version"):
        limited.open(sealed)


def test_rotation_seals_new_and_still_opens_old() -> None:
    """After rotation, new writes use the new KEK; old ciphertext still opens."""
    old = _sealer(active="v1")
    old_sealed = old.seal(TOKEN)

    rotated = _sealer(active="v2")
    new_sealed = rotated.seal(TOKEN)

    assert new_sealed.key_version == "v2"
    assert rotated.open(new_sealed) == TOKEN
    # The whole point of retaining old KEKs: no forced re-encryption downtime.
    assert rotated.open(old_sealed) == TOKEN


def test_reseal_under_new_version_changes_key_version() -> None:
    old = _sealer(active="v1")
    rotated = _sealer(active="v2")

    sealed_v1 = old.seal(TOKEN, aad=b"conn-1")
    plaintext = rotated.open(sealed_v1, aad=b"conn-1")
    resealed = rotated.seal(plaintext, aad=b"conn-1")

    assert sealed_v1.key_version == "v1"
    assert resealed.key_version == "v2"
    assert rotated.open(resealed, aad=b"conn-1") == TOKEN


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


def test_rejects_wrong_length_kek() -> None:
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        EnvelopeSealer(keys={"v1": b"too-short"}, active_key_version="v1")


def test_rejects_missing_active_version() -> None:
    with pytest.raises(ValueError, match="not present in keys"):
        EnvelopeSealer(keys={"v1": KEK_V1}, active_key_version="v9")


def test_rejects_empty_registry() -> None:
    with pytest.raises(ValueError, match="at least one KEK"):
        EnvelopeSealer(keys={}, active_key_version="v1")


def test_rejects_blank_key_version() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        EnvelopeSealer(keys={"  ": KEK_V1}, active_key_version="  ")


# ---------------------------------------------------------------------------
# Leak resistance
# ---------------------------------------------------------------------------


def test_sealed_secret_repr_redacts_ciphertext() -> None:
    sealed = _sealer().seal(TOKEN)
    rendered = repr(sealed)

    assert "bytes" in rendered
    assert "v1" in rendered
    # A traceback or log line must not dump the envelope body.
    assert sealed.ciphertext.hex()[:16] not in rendered


def test_decryption_error_message_carries_no_secret_material() -> None:
    sealer = _sealer()
    sealed = sealer.seal(TOKEN, aad=b"conn-1")

    with pytest.raises(SecretDecryptionError) as exc_info:
        sealer.open(sealed, aad=b"conn-2")

    rendered = str(exc_info.value)
    assert TOKEN not in rendered.encode()
    assert "rt-abc123" not in rendered
