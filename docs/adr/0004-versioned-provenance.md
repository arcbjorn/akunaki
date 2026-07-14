# ADR 0004: Versioned provenance

**Status:** Proposed

**Last reviewed:** 2026-07-13

## Context

Wearable vendors revise historical data; normalizers and formulas evolve; users deserve to know what produced a score. Mutable in-place updates destroy auditability. Conversely, never deleting anything conflicts with privacy rights. Unsafe polymorphic pointers (`selected_record_table` + id) break referential integrity. Mixing exact vendor transport with logical records obscures replay.

## Decision

- **Separate transport from logical raw:** `raw_payload` retains **every** exact response body (content_hash **indexed**, not a uniqueness constraint that erases repeats); `raw_objects` / **immutable** append-only `raw_revisions` carry vendor record id, observed/effective/received timestamps, content hash, revision number, **schema_version**, deletion state, and FKs to payload and optional `sync_run`. **No `normalizer_version` on raw revisions** (belongs on facts / normalization runs). Tombstone reasons: `vendor_deleted` | `privacy_delete` only—not `superseded`.
- **Webhook capture** may write `raw_payload` with `sync_run_id` null and a transport source marker (`transport_kind`) before a sync run exists. Order: insert `webhook_inbox` with null `body_payload_id` → insert `raw_payload` → set `body_payload_id`. **Only** `webhook_inbox.body_payload_id` → `raw_payload` (no reverse `raw_payload.webhook_inbox_id`; reverse lookup is through inbox).
- **Cursor, raw records, and normalization outbox commit atomically** after fetch; crash before commit retries safely; crash after commit replays normalize from outbox.
- **Vendor deletions** append durable vendor tombstones; do not silent-gap history.
- **Privacy deletion hard-scrubs** user-linked rows; privacy tombstones are transient pipeline markers only. Two artifacts: **minimal completion proof** and access-separated **restoration-suppression ledger** (HMAC selectors under dedicated deletion key; retain until backups expire + 30 days, then destroy)—replayed before restored data is served. No claim of permanent tenant-lifetime non-linkability.
- **Facts:** `fact_records` metadata header with real FKs and one-to-one typed detail tables—not core EAV; no unsafe table-name/id pointers.
- **Selections:** one current `source_selections` per `grain_key`; session/workout pin `source_grain_version_id` (membership snapshot under stable `source_grain_id`); nullable real `selected_fact_record_id`; candidates in `source_selection_candidates`; no drifting `is_authoritative` on facts.
- **Derived artifacts:** versioned rows with current-row semantics; every feature, baseline, score, factor, anomaly, and recommendation links to **`derivation_runs` / `derivation_inputs`** with **typed nullable FKs** (CHECK exactly one; no polymorphic `input_kind`/`input_id`), formula version, source-policy version/generation, dependency hash, confidence, freshness, `as_of_at`, supersession. Canonical names: `daily_health_features`, `daily_health_scores`.
- **Optional vectors (later):** `retrieval_documents` parent + `vector_embeddings` real FK; typed source links/CHECK; no generic `source_kind`/`source_id`; raw measurements out by default; tenant predicate mandatory; delete/rebuild lineage.
- Reads use current rows; history and provenance APIs expose lineage.
- JSON is used only where flexibility is justified; core metrics use typed columns; registry key/value only for feature codes and long nutrient/lab vocabularies with enforced definitions.

## Consequences

### Positive

- Reprocessing and formula rollback without lying about history
- Why UI can show real lineage and coverage
- Idempotent ingestion via content hash
- Referential integrity for selections and facts
- Crash-safe sync commit boundaries

### Negative

- More tables, rows, and careful indexes
- Application must always set version and derivation metadata
- Storage growth requires retention policy
- Hard-scrub + dual deletion artifacts (completion proof + restoration-suppression ledger) complexity for privacy

### Neutral

- Export includes versions needed for user portability
- Schema migrations (Alembic) are separate from formula migrations (recompute)

## Reversal conditions

Revisit storage mechanics if:

1. Storage cost of full raw payloads becomes untenable—then introduce tiered raw retention **without** dropping dependency hashes on scores.
2. A compliance regime requires stronger cryptographic ledgering—extend, do not remove, versioning.

Do not reverse into silent in-place mutation of scores or raw payloads for convenience. Do not reintroduce unsafe table-name pointers for selections.

## Related

- [../architecture/data-model.md](../architecture/data-model.md)
- [../architecture/ingestion-and-sync.md](../architecture/ingestion-and-sync.md)
- [../architecture/health-engine.md](../architecture/health-engine.md)
