# Phase Zero evidence: application-level envelope encryption

**Date:** 2026-07-18

**Status:** Partial — application-level envelope encryption for secret columns is implemented and tested; **external KMS/secret-manager sourcing, backup/export encryption, and rotation runbooks are not implemented**

**Authoritative context:** [security.md](../architecture/security.md) key management, [data-model.md](../architecture/data-model.md) `connection_secrets`, [ingestion-and-sync.md](../architecture/ingestion-and-sync.md) OAuth token handling

---

## Scheme implemented (format v1)

Per `seal` call:

1. Generate a fresh random 256-bit **DEK**.
2. AES-256-GCM encrypt the plaintext under the DEK with a fresh 96-bit nonce, binding the caller's **AAD**.
3. AES-256-GCM wrap the DEK under the active **KEK** with its own fresh nonce, binding `key_version` as AAD so a wrapped DEK cannot be relabelled to another version.
4. Serialize format byte, key version, both nonces, the wrapped DEK, and the payload into one opaque blob stored in a `BLOB` column.

A DEK encrypts exactly one message, so GCM nonce reuse cannot occur across records even if a nonce repeats by chance under a different key.

| Parameter | Value |
|-----------|-------|
| AEAD | AES-256-GCM (`cryptography==49.0.0`) |
| KEK / DEK size | 32 bytes (AES-256) |
| Nonce size | 12 bytes, freshly random per seal (both layers) |
| Randomness source | `os.urandom` |
| DEK reuse | None — one DEK per message |
| AAD | Caller-supplied (connection id in practice); bound to the payload |
| Key source | `AKUNAKI_SECRET_KEKS` as `version:base64key` pairs; **no default, no generated fallback** |

---

## Invariants proven

| Invariant | Test coverage |
|-----------|---------------|
| Seal/open round trip, including empty, 256 KiB, and full-byte-range payloads | `test_seal_open_roundtrip`, `test_empty_and_large_payloads_roundtrip`, `test_binary_payload_roundtrips_exactly` |
| Ciphertext contains no readable plaintext | `test_ciphertext_does_not_contain_plaintext` |
| Identical plaintexts produce distinct **payloads** and distinct DEK nonces (no equality oracle) | `test_identical_plaintexts_have_distinct_payloads_and_nonces` |
| A fresh DEK per seal (verified by unwrapping, not by comparing wrapped bytes) | `test_each_seal_uses_a_fresh_dek` |
| Fresh KEK nonce per seal | `test_kek_nonce_is_fresh_per_seal` |
| AAD round trip; wrong, missing, or unexpected AAD all fail | `test_aad_roundtrip`, `test_wrong_aad_fails`, `test_missing_aad_fails_when_sealed_with_aad`, `test_unexpected_aad_fails_when_sealed_without` |
| Any flipped bit, truncation, garbage input, or bumped format byte fails | `test_flipped_bit_anywhere_fails` (parametrized), `test_truncated_ciphertext_fails`, `test_garbage_ciphertext_fails`, `test_unsupported_format_byte_fails` |
| Column `key_version` must agree with the envelope's own label | `test_key_version_label_mismatch_fails` |
| A wrapped DEK spliced from another envelope does not open | `test_wrapped_dek_cannot_be_swapped_between_envelopes` |
| Wrong KEK and unknown key version fail cleanly | `test_wrong_kek_cannot_open`, `test_unknown_key_version_reports_cleanly` |
| Rotation seals new envelopes under the new KEK while old ciphertext still opens | `test_rotation_seals_new_and_still_opens_old`, `test_reseal_under_new_version_changes_key_version` |
| Construction rejects wrong-length KEK, missing active version, empty registry, blank version | `test_rejects_wrong_length_kek`, `test_rejects_missing_active_version`, `test_rejects_empty_registry`, `test_rejects_blank_key_version` |
| `SealedSecret` repr and `SecretDecryptionError` carry no secret material | `test_sealed_secret_repr_redacts_ciphertext`, `test_decryption_error_message_carries_no_secret_material` |

### KEK configuration (fail fast)

| Invariant | Test coverage |
|-----------|---------------|
| Strict `version:base64key` parsing; whitespace tolerated | `test_parses_single_kek`, `test_parses_multiple_keks_and_tolerates_whitespace` |
| Missing separator, blank version/key, invalid base64, wrong length, duplicate version all rejected | `test_rejects_missing_separator`, `test_rejects_blank_version_or_key`, `test_rejects_invalid_base64`, `test_rejects_wrong_key_length`, `test_rejects_duplicate_version` |
| Config errors name the version only, never key bytes | `test_parse_errors_never_leak_key_material` |
| No configured KEK → boot refuses to build a sealer (never a silent no-op) | `test_build_sealer_fails_fast_without_configuration` |
| Active version inferred only when unambiguous; required when multiple KEKs exist | `test_build_sealer_with_single_key_infers_active_version`, `test_build_sealer_requires_explicit_active_when_multiple` |
| Settings ship no default keys | `test_settings_default_has_no_keys` |

### Persistence (`connection_secrets`)

| Invariant | Test coverage |
|-----------|---------------|
| Sealed token round trips through the real database | `test_sealed_token_roundtrips_through_the_database` |
| Raw stored column (read outside the ORM) contains no readable token | `test_stored_bytes_contain_no_readable_token` |
| An envelope copied onto another connection's row does not open (AAD binding) | `test_envelope_cannot_be_moved_to_another_connection` |
| Stored `key_version` matches the envelope | `test_key_version_column_matches_envelope` |
| Re-encrypt-on-read rotation updates the row's key version | `test_rotation_reseals_row_under_new_key_version` |
| Deleting a connection deletes its ciphertext (no orphaned secrets) | `test_secret_row_is_deleted_with_its_connection` |
| The settings-configured sealer (not just a test fixture) works end to end | `test_settings_configured_sealer_persists_and_reopens` |

---

## Mutation checks performed

Passing crypto tests are weak evidence on their own, so the randomness guarantees were verified by deliberately breaking the implementation:

| Mutation | Result |
|----------|--------|
| Fixed DEK (`b"K"*32`) | **Caught** by `test_each_seal_uses_a_fresh_dek` |
| Fixed DEK nonce | **Caught** by `test_identical_plaintexts_have_distinct_payloads_and_nonces` |
| Fixed KEK nonce | **Caught** by `test_kek_nonce_is_fresh_per_seal` |
| Payload stored as plaintext | **Caught** (9 tests fail) |

An earlier revision of these tests **missed** the fixed-DEK and fixed-nonce mutations: comparing whole ciphertexts passed because the random KEK nonce in the header masked an identical payload. The tests now assert on the parsed payload, DEK nonce, KEK nonce, and unwrapped DEK separately. Recorded because the weak version looked correct.

---

## Honest scope

| In scope | Out of scope (not claimed) |
|----------|----------------------------|
| AES-256-GCM envelope for secret columns | External KMS / secret-manager KEK sourcing |
| KEK/DEK hierarchy with versioned registry | Automated or scheduled rotation; rotation runbooks |
| Rotation semantics (new writes on new KEK; old ciphertext still readable) | Bulk re-encryption job over existing rows |
| AAD binding an envelope to its owning row | HSM / hardware-backed keys |
| Fail-fast boot without configured keys | Backup, export, and object-storage encryption |
| Local `AKUNAKI_SECRET_KEKS` configuration | Key access separation, audit logging of key use |
| `connection_secrets` persistence | `oauth_states` PKCE verifier sealing (schema not yet added) |

---

## What this evidence does *not* claim

- Turso platform encryption at rest (provider-side; deferred with remote Turso)
- Backup/restore key separation or the restoration-suppression deletion key
- Any OAuth flow, token acquisition, or refresh path
- Key rotation operations at scale, or re-encryption batch tooling
- Formal cryptographic review or third-party audit of this construction

---

## Related

- [phase-zero-turso-foundation.md](phase-zero-turso-foundation.md) (note 4: libSQL BLOB binding limitation this depends on)
- [../architecture/security.md](../architecture/security.md)
- [../implementation-status.md](../implementation-status.md)
