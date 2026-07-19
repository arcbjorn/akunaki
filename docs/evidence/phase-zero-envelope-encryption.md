# Phase Zero evidence: application-level envelope encryption

**Date:** 2026-07-19

**Status:** Partial — application-level envelope encryption, the OAuth state/PKCE handshake, the Oura OAuth client, and the end-to-end **linking service** are implemented and tested; **external KMS/secret-manager sourcing, backup/export encryption, rotation runbooks, and the HTTP routes (deferred pending auth) are not implemented**

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

### OAuth state and PKCE (`oauth_states`)

The authorize/callback handshake is the first consumer of the sealer. Security rules live in `OAuthStateRepository`, not in callers.

| Invariant | Test coverage |
|-----------|---------------|
| Raw `state` is never stored; only its SHA-256 hash | `test_raw_state_is_never_stored`, `test_state_hash_hides_the_raw_state` |
| PKCE verifier is stored sealed and unreadable in the row | `test_raw_state_is_never_stored` |
| `code_challenge` matches RFC 7636 **S256** (recomputed independently) | `test_code_challenge_matches_rfc7636_s256` |
| Verifier length within the RFC 7636 43–128 range; unreserved chars only | `test_generated_verifier_is_unique_and_rfc_compliant`, `test_code_challenge_rejects_out_of_range_verifier` |
| State and verifier are unique per generation (256-sample) | `test_generated_state_is_unique_and_high_entropy`, `test_generated_verifier_is_unique_and_rfc_compliant` |
| State comparison is constant-time and matches only the original value | `test_state_matches_only_for_the_original_value` |
| A valid consume releases the sealed verifier | `test_valid_consume_returns_the_sealed_verifier` |
| **Single use:** a replayed callback is rejected | `test_state_is_single_use` |
| Expiry enforced; boundary is exclusive | `test_expired_state_is_rejected`, `test_expiry_boundary_is_exclusive` |
| Redirect URI matched **exactly** (no trailing slash, query, scheme, case, or suffix tolerance) | `test_redirect_uri_must_match_exactly`, `test_redirect_uri_match_is_exact` |
| A failed attempt does **not** burn the state (no DoS vector) | `test_redirect_mismatch_does_not_consume_the_state` |
| Unknown/forged state and empty inputs rejected | `test_unknown_state_is_rejected`, `test_empty_inputs_are_rejected` |
| Duplicate `state_hash` rejected by unique constraint | `test_duplicate_state_hash_is_rejected` |
| Concurrent callbacks yield exactly one winner | `test_concurrent_consume_yields_exactly_one_winner` |
| Expiry purge removes spent rows, retaining live ones | `test_purge_removes_expired_and_keeps_live_states`, `test_purge_clears_spent_verifier_ciphertext` |
| Model/migration agreement; verifier column is BLOB; no raw `state` column | `test_oauth_state_model_matches_migration` |
| States cascade with their tenant | `test_states_cascade_with_tenant` |

### Oura OAuth client

All traffic is served by an in-process mock transport or a local `http.server`; **no test touches the real Oura API.**

| Invariant | Test coverage |
|-----------|---------------|
| Authorize URL carries state, challenge, exact redirect, and `code_challenge_method=S256` | `test_authorize_url_carries_pkce_s256_and_state` |
| Authorize URL never carries the client secret | `test_authorize_url_never_contains_the_client_secret` |
| Code exchange sends the PKCE verifier and returns parsed tokens | `test_exchange_code_sends_pkce_verifier_and_returns_tokens` |
| Relative `expires_in` converted to an absolute instant (survives restart) | `test_exchange_code_sends_pkce_verifier_and_returns_tokens` |
| Refresh uses the `refresh_token` grant | `test_refresh_sends_refresh_grant` |
| Missing optional response fields tolerated | `test_missing_optional_fields_are_tolerated` |
| `invalid_grant` / `invalid_client` / `unauthorized_client` are **non-retryable** (drive reauth) | `test_permanent_provider_errors_are_not_retryable` |
| 5xx and transport failures are **retryable** | `test_server_error_is_retryable`, `test_transport_error_is_retryable` |
| Non-JSON and missing `access_token` responses are malformed, not silent successes | `test_non_json_response_is_malformed`, `test_response_without_access_token_is_malformed` |
| Argument and credential validation | `test_exchange_validates_arguments`, `test_construction_requires_credentials`, `test_authorize_url_validates_arguments` |
| Client repr, token repr, and both log paths carry no secrets | `test_client_repr_redacts_credentials`, `test_tokens_repr_redacts_token_values`, `test_error_logs_never_contain_secrets_or_bodies`, `test_transport_error_logs_no_request_body` |

### OAuth linking service (end-to-end orchestration)

Wired against the **real** state repository, sealer, connection repository, and Oura client over a mock transport — not a stack of doubles. HTTP routes are deliberately deferred (see below), so `tenant_id` is a service parameter.

| Invariant | Test coverage |
|-----------|---------------|
| Full authorize → callback flow links the connection and stores sealed tokens | `test_full_link_flow_persists_sealed_tokens` |
| Stored token ciphertext (read outside the ORM) contains no readable token | `test_full_link_flow_persists_sealed_tokens` |
| The authorize URL's S256 challenge matches the verifier that was actually sealed | `test_authorize_url_uses_s256_challenge_for_the_stored_verifier` |
| Replayed, forged, expired, and redirect-mismatched callbacks all rejected | `test_replayed_callback_is_rejected`, `test_forged_state_is_rejected`, `test_expired_state_is_rejected`, `test_mismatched_redirect_is_rejected` |
| A provider-denied callback (no `code`) does **not** burn the state | `test_missing_code_does_not_consume_the_state` |
| An invalid state creates no connection and no secret | `test_no_connection_is_created_when_state_is_invalid` |
| `invalid_grant` is non-retryable (drives reauth); 5xx/transport is retryable | `test_invalid_grant_is_not_retryable`, `test_provider_outage_is_retryable` |
| A failed exchange leaves **no half-written connection** | `test_failed_exchange_leaves_no_half_written_connection` |
| An unopenable sealed verifier is reported distinctly, not as a provider error | `test_unreadable_verifier_is_reported_distinctly` |
| Re-consent reuses the existing connection row (no duplicate per provider) | `test_relinking_reuses_the_connection_row` |
| `needs_reauth` transition; unknown connection returns False | `test_mark_needs_reauth_transitions_status`, `test_mark_needs_reauth_on_unknown_connection_returns_false` |

**Atomicity verified by fault injection.** Raising between the connection-row write and the secret write rolls back **both** (0 rows of each), so an `active` connection can never exist without usable token material.

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

### Oura OAuth client secret handling

| Mutation | Result |
|----------|--------|
| Log the full provider error body | **Caught** by `test_error_logs_never_contain_secrets_or_bodies` |
| Log the request payload on transport error | **Caught** by `test_transport_error_logs_no_request_body` |
| Unredacted `OuraOAuthClient.__repr__` | **Caught** by `test_client_repr_redacts_credentials` |
| Remove `OAuthTokens.__repr__` redaction | **Caught** by `test_tokens_repr_redacts_token_values` |

**Vacuous-test finding.** The two log-leak tests initially passed in isolation but failed once run after the migration tests — and the failure was in the *positive control*, not the leak assertion: `caplog` had captured **zero** records, so `assert secret not in rendered` was passing against an empty string. Any leak would have gone undetected.

Root cause: Alembic's `env.py` calls `logging.config.fileConfig`, which replaces the root handlers and removes pytest's capture handlers for the rest of the session. Two fixes were applied — `fileConfig(..., disable_existing_loggers=False)` in `env.py`, and a private handler attached directly to the connector logger in the tests instead of `caplog`, so the assertions no longer depend on global logging state. Every leak test now carries an explicit positive control asserting that something *was* captured.

Recorded because the weak version passed a full green suite.

### OAuth linking orchestration

| Mutation | Result |
|----------|--------|
| Skip state validation on callback | **Caught** (5 tests fail) |
| Link even when the token exchange failed | **Caught** (3 tests fail) |
| Treat `invalid_grant` as retryable | **Caught** by `test_invalid_grant_is_not_retryable` |
| Remove the missing-`code` guard | **Caught** by `test_missing_code_does_not_consume_the_state` |
| Raise between connection-row and secret writes | **Rolled back cleanly** — 0 connections, 0 secrets |

### OAuth state enforcement

| Mutation | Result |
|----------|--------|
| Skip expiry check | **Caught** (2 tests fail) |
| Skip exact redirect-URI match | **Caught** (2 tests fail) |
| Remove read-side `consumed_at` check **only** | Survives — the atomic CAS still enforces single use |
| Remove atomic CAS guard **only** | Survives at natural timing — see caveat below |
| Remove **both** single-use guards | **Caught** (`test_state_is_single_use`, `test_concurrent_consume_yields_exactly_one_winner`) |

**Single-use timing caveat.** Consumption is guarded twice: a read-side `consumed_at` check and an atomic conditional `UPDATE ... WHERE consumed_at IS NULL`. These are deliberate defense-in-depth, so removing either alone still passes — the other covers it.

Tracing confirms all concurrent threads *do* read the row as unconsumed, so the race window is genuinely open. However, **libSQL serializes the write transactions**, so at natural timing the read-side check alone breaks the tie and the concurrency test passes even with the CAS guard removed. Artificially widening the read→write window makes that test **fail without the CAS** and **pass with it**, confirming the CAS is load-bearing on any store that does not serialize writes.

This is the same class of masking already recorded for concurrent enqueue in [phase-zero-job-concurrency.md](phase-zero-job-concurrency.md): local libSQL write serialization hides races that a networked store would expose. The caveat is documented in the test docstring itself.

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
| `oauth_states` single-use handshake (hashed state, sealed PKCE verifier, exact redirect, expiry) | HTTP authorize/callback endpoints; provider registration |
| Oura OAuth client: authorize URL, PKCE code exchange, refresh, typed failure mapping | Google Health / Polar OAuth clients |
| OAuth linking service: start link, callback validation, sealed token persistence, relink | HTTP authorize/callback routes (deferred pending auth); session binding; CSRF |
| `connection_secrets` persistence | Provider OAuth clients, token exchange, or refresh |

---

## What this evidence does *not* claim

- Turso platform encryption at rest (provider-side; deferred with remote Turso)
- Backup/restore key separation or the restoration-suppression deletion key
- HTTP authorize/callback endpoints (**deliberately deferred**, see below)
- Any authenticated `/v1` surface, session cookies, or CSRF
- Any live call against the real Oura API (all tests use a mock transport or a local HTTP server)
- Google Health and Polar OAuth clients
- Key rotation operations at scale, or re-encryption batch tooling
- Formal cryptographic review or third-party audit of this construction

---

## Deferred: HTTP OAuth routes

The `/v1/connections/{provider}/oauth/start` and `/callback` endpoints are **not** implemented, by decision rather than omission. Those routes are authenticated in the design — they need a `tenant_id` from a session — and auth/OIDC is not built. Shipping them now would mean either an unauthenticated `/v1` surface or a throwaway auth shim.

Instead the whole flow lives in `OAuthLinkingService` with `tenant_id` as an explicit parameter, fully tested against real components. Once sessions exist, the routes become a thin layer that resolves the tenant and calls the service; none of the security rules above move.

---

## Related

- [phase-zero-turso-foundation.md](phase-zero-turso-foundation.md) (note 4: libSQL BLOB binding limitation this depends on)
- [../architecture/security.md](../architecture/security.md)
- [../implementation-status.md](../implementation-status.md)
