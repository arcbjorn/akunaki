# Data model

**Status:** Proposed

**Last reviewed:** 2026-07-13

Authoritative for **database schema** and co-authoritative for **raw/normalized models** and **migrations** (coverage matrix items 3, 5, 18). Target store: SQLAlchemy 2 models on **libSQL/SQLite** (dev/CI relational) and **Turso** (**selected** production operational store—see [ADR 0003](../adr/0003-libsql-operational-store.md)). Phase zero validates the exact driver/migration/concurrency/encryption/volume path; only a proven blocker reopens the ADR. No DuckDB in MVP. No generic EAV table for core metrics.

Nothing here is deployed; names are proposed.

---

## Physical conventions (SQLite / libSQL)

These are **physical SQL types** as stored in SQLite/libSQL. SQLAlchemy Python types map onto them; do not invent PostgreSQL-only types in schema docs.

| Convention | Physical rule | SQLAlchemy Python mapping (proposed) |
|------------|---------------|--------------------------------------|
| Primary / foreign keys | `TEXT` holding **UUIDv7** (canonical string form) | `Uuid` / `str` with application UUIDv7 generator; stored as `Text` |
| Timestamps | `TEXT` fixed **UTC RFC3339** with `Z` suffix, second or millisecond precision consistent per column family | `DateTime(timezone=True)` coerced to UTC string on bind, or plain `str` |
| Local calendar dates | `TEXT` `YYYY-MM-DD` (local health day, as-of day) | `date` → ISO date string |
| Booleans | `INTEGER` `0` or `1` with `CHECK (col IN (0,1))` | `bool` |
| Integers | `INTEGER` | `int` |
| Floats | `REAL` | `float` |
| Enums / short codes | `TEXT` with app-level or CHECK constraints | `str` / `Enum` |
| JSON | `TEXT` canonical UTF-8 JSON with `CHECK (json_valid(col))` where non-null | `dict`/`list` via SQLAlchemy JSON type configured for text storage |
| Binary ciphertext | `BLOB` | `bytes` |
| Foreign keys | **`PRAGMA foreign_keys = ON`** on every connection | SQLAlchemy `ForeignKey` |
| Arrays | **No** native text-array type. Use JSON `TEXT` arrays with `json_valid`, or child tables | `list[str]` as JSON text |

### Naming notes

- Document columns as physical SQL types above, not `UUID`, `timestamptz`, or `text[]`.
- `oidc_issuer` is a real column on `users` (paired with `oidc_subject`).
- Composite uniqueness and FKs almost always include `tenant_id` for tenant isolation.

---

## Design principles

1. **Tenant scoping:** almost every table includes `tenant_id` with composite uniqueness that includes it.
2. **Separate vendor transport from logical records:** exact bodies in `raw_payload`; logical identity in `raw_objects` / `raw_revisions`.
3. **Append-only raw revisions** with vendor tombstones; privacy deletion hard-scrubs user-linked rows.
4. **Typed facts:** `fact_records` common header + one-to-one typed detail tables—not core EAV; not unsafe table-name/id string pointers.
5. **Required `source_selections`:** exactly one current versioned decision per non-null provider-independent `grain_key` (stable `source_grains.id` for session/workout; pin exact membership via `source_grain_version_id` + members); nullable real FK to `fact_records`; alternatives only in `source_selection_candidates`.
6. **Reproducible derivation lineage** via `derivation_runs` / `derivation_inputs` with **typed nullable FKs** (no polymorphic `input_kind`/`input_id`).
7. **JSON only where justified** (raw payload, redacted request meta, sparse device metadata, policy params, long vocab registries)—not for core metric values.
8. **Registry key/value** acceptable only for derived feature codes and long nutrient/lab vocabularies with enforced definition rows.

---

## Identity and tenancy

### `tenants`

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | UUIDv7 |
| `created_at` | TEXT | UTC RFC3339 |
| `status` | TEXT | `active`, `suspended`, `pending_delete` |
| `primary_timezone` | TEXT | IANA, default `UTC` until user sets |
| `display_name` | TEXT NULL | **sensitive PII**; not free log material; treat as sensitive (not “non-PHI”) |

### `users`

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | UUIDv7 |
| `tenant_id` | TEXT FK → tenants | unique per MVP single-user tenant |
| `oidc_issuer` | TEXT | issuer URL; **required** with subject for uniqueness |
| `oidc_subject` | TEXT | issuer-unique subject |
| `email` | TEXT NULL | from IdP; **sensitive PII**; not used as PK; never free log material |
| `created_at` | TEXT | UTC RFC3339 |
| Unique | `(oidc_issuer, oidc_subject)` | |

### `sessions`

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | session row id (UUIDv7); not the cookie value |
| `user_id` | TEXT FK | |
| `tenant_id` | TEXT FK | denormalized for authz |
| `token_hash` | TEXT | **hashed** opaque session cookie token; **never** store raw cookie token |
| `csrf_secret_hash` | TEXT | **hashed** CSRF secret, or store a server-derived key id from which the CSRF secret is derived—never plaintext CSRF secret |
| `created_at`, `expires_at` | TEXT | UTC RFC3339 |
| `revoked_at` | TEXT NULL | |
| Unique | `(token_hash)` | lookup by presented cookie hash |

---

## Connections and OAuth

### `connections`

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `tenant_id` | TEXT | |
| `provider` | TEXT | `oura`, `google_health`, `polar` |
| `status` | TEXT | `pending`, `active`, `needs_reauth`, `revoked`, `error` |
| `scopes_granted_json` | TEXT | JSON array of scope strings; `json_valid` |
| `external_user_id` | TEXT NULL | provider subject |
| `connected_at`, `updated_at` | TEXT | UTC RFC3339 |
| Unique | `(tenant_id, provider)` | one connection per provider per tenant MVP |

Tokens are **not** stored on this table.

### `connection_secrets`

| Column | Physical type | Notes |
|--------|---------------|-------|
| `connection_id` | TEXT PK/FK | |
| `tenant_id` | TEXT | |
| `ciphertext` | BLOB | refresh/access tokens envelope-encrypted |
| `key_version` | TEXT | KEK version |
| `rotated_at` | TEXT | UTC RFC3339 |

### `connection_health`

| Column | Physical type | Notes |
|--------|---------------|-------|
| `connection_id` | TEXT PK/FK | |
| `tenant_id` | TEXT | |
| `last_success_at` | TEXT NULL | |
| `last_error_class` | TEXT NULL | no payload bodies |
| `consecutive_failures` | INTEGER | |
| `rate_limit_reset_at` | TEXT NULL | |
| `webhook_last_verified_at` | TEXT NULL | |

### `oauth_states`

Short-lived OAuth CSRF/PKCE state. Single-use; expire and purge after `expires_at`.

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `tenant_id` | TEXT | |
| `provider` | TEXT | |
| `state_hash` | TEXT | unique; **hashed** OAuth `state` (never store raw state) |
| `code_verifier_ciphertext` | BLOB | envelope-encrypted PKCE `code_verifier` |
| `code_verifier_key_version` | TEXT | KEK version for verifier ciphertext |
| `redirect_uri` | TEXT | **exact** redirect URI used at authorize; must match callback |
| `created_at`, `expires_at` | TEXT | UTC RFC3339; enforce expiry |
| `consumed_at` | TEXT NULL | set on successful consume; single-use (reject if already set) |
| Unique | `(state_hash)` | |

---

## Sync transport layer

### `sync_runs`

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `tenant_id` | TEXT | |
| `connection_id` | TEXT FK | |
| `trigger` | TEXT | `schedule`, `webhook`, `manual`, `reconcile`, `initial` |
| `stream` | TEXT NULL | null = multi-stream run |
| `status` | TEXT | `running`, `succeeded`, `failed`, `partial` |
| `started_at`, `finished_at` | TEXT NULL | |
| `error_class` | TEXT NULL | no bodies |
| `webhook_inbox_id` | TEXT NULL FK | when triggered by webhook |
| `stats_json` | TEXT NULL | counts only; `json_valid` |

### `raw_payload`

Exact vendor transport pages. **Every response body is retained** (including identical content on retries and webhook captures).

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `tenant_id` | TEXT | |
| `connection_id` | TEXT FK | |
| `sync_run_id` | TEXT NULL FK → sync_runs | **nullable**: webhook body capture may land before a sync run exists |
| `transport_kind` | TEXT | e.g. `sync_fetch`, `webhook_capture` (source marker; not an FK) |
| `provider` | TEXT | |
| `stream` | TEXT | |
| `page_token` | TEXT NULL | |
| `fetched_at` | TEXT NULL | UTC; null for pure webhook captures until refetch |
| `received_at` | TEXT | UTC |
| `http_status` | INTEGER NULL | |
| `content_type` | TEXT NULL | |
| `content_hash` | TEXT | sha256 of exact body; **indexed for lookup**, **not** a uniqueness constraint |
| `payload_json` | TEXT NULL | exact JSON body; `json_valid`; mutually exclusive with blob |
| `payload_blob` | BLOB NULL | non-JSON bodies |
| `request_meta_json` | TEXT | redacted URL template id, params; **no secrets**; `json_valid` |

**CHECK:** exactly one of `payload_json` / `payload_blob` non-null when body present.

**Index (not unique):** `(connection_id, content_hash)`, `(tenant_id, received_at)`.

Repeat responses with the same hash create **new** `raw_payload` rows. Logical-revision dedupe (no new `raw_revisions` when logical content unchanged) is separate and never deletes transport rows.

**No reverse FK from `raw_payload` to `webhook_inbox`.** Body ownership is one-way: `webhook_inbox.body_payload_id` → `raw_payload`. Reverse lookup (payload → inbox) is through `webhook_inbox` by `body_payload_id`.

#### Webhook payload capture order (one-way FK only)

1. Verify webhook; insert `webhook_inbox` with `body_payload_id` null, status `accepted`.
2. Insert `raw_payload` with `sync_run_id` null, `transport_kind = webhook_capture` (or equivalent source marker).
3. Update `webhook_inbox.body_payload_id` to the new payload id.
4. Enqueue refetch; worker creates `sync_runs` and may attach additional fetch pages with `sync_run_id` set and `transport_kind = sync_fetch`.
5. Optional: update the capture row’s `sync_run_id` when a later run associates it.

Do **not** require `sync_run_id` non-null on every `raw_payload`. Do **not** add `raw_payload.webhook_inbox_id` (that would reintroduce a bidirectional FK cycle with `body_payload_id`).

### `sync_cursors`

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `tenant_id` | TEXT | |
| `connection_id` | TEXT FK | |
| `stream` | TEXT | e.g. `sleep`, `heartrate_intraday`, `exercises` |
| `cursor_type` | TEXT | `timestamp`, `page_token`, `resource_id` |
| `cursor_value` | TEXT | opaque |
| `window_start`, `window_end` | TEXT NULL | last successful window (UTC RFC3339) |
| `updated_at` | TEXT | |
| Unique | `(connection_id, stream)` | |

### `webhook_inbox`

Durable deduplicated webhook deliveries.

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `tenant_id` | TEXT | |
| `connection_id` | TEXT FK | |
| `provider` | TEXT | |
| `delivery_id` | TEXT NULL | vendor delivery id when present |
| `dedupe_key` | TEXT | unique per connection (delivery id or hash) |
| `received_at` | TEXT | |
| `verified_at` | TEXT | |
| `headers_meta_json` | TEXT | redacted; `json_valid` |
| `body_payload_id` | TEXT NULL FK → raw_payload | sole FK between inbox and payload; set after payload insert (see capture order) |
| `processing_status` | TEXT | `accepted`, `enqueued`, `processed`, `ignored_dup` |
| Unique | `(connection_id, dedupe_key)` | |

### Atomic fetch commit and crash replay

After fetch, **one transaction** commits: `raw_payload` page(s) with `sync_run_id`, `raw_objects`/`raw_revisions`, `sync_cursors`, normalization outbox/jobs, `sync_run` progress. Crash before commit → retry same window; **retain every transport response**; logical revisions skip append when logical `content_hash` already present for that object. Crash after commit → outbox drives normalize. Webhook capture uses the separate order above (no `sync_run` required). Details: [ingestion-and-sync.md](ingestion-and-sync.md).

---

## Logical raw layer (append-only revisions)

### `raw_objects`

Logical identity of a vendor record.

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `tenant_id` | TEXT | |
| `connection_id` | TEXT FK | |
| `provider` | TEXT | |
| `stream` | TEXT | |
| `vendor_record_id` | TEXT | stable vendor id or hash of natural key |
| `current_revision_id` | TEXT NULL FK → raw_revisions | latest non-privacy-scrubbed revision |
| `created_at` | TEXT | first seen |
| Unique | `(tenant_id, provider, stream, vendor_record_id)` | |

### `raw_revisions`

**Immutable** logical versions linked to transport and run. Never updated in place after insert (except transient privacy-pipeline markers that hard-scrub replaces).

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `tenant_id` | TEXT | |
| `raw_object_id` | TEXT FK | |
| `raw_payload_id` | TEXT FK | exact body page |
| `sync_run_id` | TEXT NULL FK | null only for pure webhook-linked logical rows when allowed by stream policy; usually set after sync |
| `revision_n` | INTEGER | monotonic per object |
| `vendor_record_id` | TEXT | denormalized |
| `observed_at` | TEXT NULL | vendor-observed timestamp UTC |
| `effective_at` | TEXT NULL | effective time for the record UTC |
| `received_at` | TEXT | ingest time UTC |
| `content_hash` | TEXT | sha256 of logical body slice or full page as defined per stream |
| `schema_version` | TEXT | vendor/envelope schema |
| `deletion_state` | TEXT | `active`, `vendor_deleted`, `privacy_scrubbed` |
| `is_tombstone` | INTEGER | 0/1 CHECK; vendor or privacy marker |
| `tombstone_reason` | TEXT NULL | **`vendor_deleted`** or **`privacy_delete` only**—**not** `superseded` |
| Unique | `(raw_object_id, revision_n)` | |
| Index (not unique) | `(raw_object_id, content_hash)` | lookup; application skips new revision when hash already present and not tombstone |

**No `normalizer_version` on raw revisions.** Raw rows are immutable transport/logical snapshots. `normalizer_version` belongs on `fact_records` and on normalization/derivation run metadata when a normalizer produces facts.

**Vendor deletions:** retain tombstone revisions (`deletion_state=vendor_deleted`, `tombstone_reason=vendor_deleted`) so history is not a silent gap.

**Privacy deletion:** ultimately **hard-scrubs** user-linked payloads, facts, and derived rows. Privacy tombstones are **transient** only during the deletion pipeline; they are not a long-term substitute for scrub. See Deletion artifacts below.

---

## Devices and origin

### `devices`

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `tenant_id` | TEXT | |
| `provider` | TEXT | |
| `vendor_device_id` | TEXT NULL | |
| `data_source_id` | TEXT NULL | Google Health DataSource id |
| `data_source_family` | TEXT NULL | e.g. `google-wearables` |
| `origin_label` | TEXT NULL | e.g. Fitbit device marketing label for UI |
| `model`, `manufacturer` | TEXT NULL | |
| `meta_json` | TEXT NULL | sparse; `json_valid` |
| Unique | `(tenant_id, provider, vendor_device_id)` WHERE vendor_device_id NOT NULL | |

---

## Facts: common header + typed details

### `fact_records` (metadata header)

Every normalized measurement has exactly one header row. Metric values live in **one-to-one typed detail tables** keyed by `fact_record_id` (PK/FK). **Not** core EAV. **Not** `selected_record_table` string pointers.

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `tenant_id` | TEXT | |
| `connection_id` | TEXT NULL FK | null for pure manual if no connection |
| `provider` | TEXT | `oura`, `google_health`, `polar`, `manual`, `derived` |
| `entity_type` | TEXT | e.g. `heart_rate_sample`, `sleep_session`, … |
| `vendor_record_id` | TEXT NULL | |
| `device_id` | TEXT NULL FK → devices | |
| `origin` | TEXT NULL | DataSource / origin code |
| `method` | TEXT | `wearable`, `user_entered`, `lab`, `derived` |
| `utc_instant` | TEXT NULL | sample time UTC; sessions may use start |
| `start_utc`, `end_utc` | TEXT NULL | interval entities |
| `source_offset_minutes` | INTEGER NULL | |
| `iana_timezone` | TEXT NULL | |
| `local_health_day` | TEXT NULL | `YYYY-MM-DD` |
| `unit` | TEXT NULL | canonical unit when single-valued |
| `quality` | TEXT | `high`, `medium`, `low`, `unknown` |
| `confidence` | REAL | 0–1 |
| `freshness_at` | TEXT NULL | when source last confirmed |
| `raw_revision_id` | TEXT NULL FK | lineage |
| `raw_payload_id` | TEXT NULL FK | |
| `schema_version` | TEXT | |
| `normalizer_version` | TEXT | |
| `content_hash` | TEXT NULL | normalized content hash |
| `version_n` | INTEGER | |
| `is_current` | INTEGER | 0/1 CHECK |
| `superseded_by` | TEXT NULL | |
| `superseded_at` | TEXT NULL | |
| `deletion_state` | TEXT | `active`, `vendor_deleted`, `privacy_scrubbed` |
| `exclude_from_load` | INTEGER | 0/1; Google/Fitbit-origin workout overlap with Polar |
| `created_at` | TEXT | |

Indexes: `(tenant_id, entity_type, local_health_day, is_current)`, `(tenant_id, raw_revision_id)`, composite FKs with `tenant_id` where enforced by app or composite FK pattern.

### Typed detail tables (one-to-one with `fact_records`)

Each detail table: `fact_record_id TEXT PK FK → fact_records(id)`, `tenant_id TEXT`, plus typed columns. Required entity types:

#### Samples and vitals

| Table | Core columns (typed) |
|-------|----------------------|
| `heart_rate_samples` | `bpm REAL`, sample role (`resting`, `intraday`, `workout`, …) |
| `hrv_samples` | `rmssd_ms REAL NULL`, `sdnn_ms REAL NULL`, `window_seconds INTEGER NULL`, `statistic_code TEXT` |
| `oxygen_saturation_samples` | `spo2_pct REAL` (canonical table name; entity_type may use `oxygen_saturation_sample`) |
| `temperature_samples` | `temperature_c REAL`, `site_code TEXT NULL` |
| `respiratory_samples` | `breaths_per_min REAL` |
| `activity_samples` | `steps_count INTEGER NULL` (steps are **INTEGER**), `energy_kcal REAL NULL` (canonical energy unit **kcal**), `distance_m REAL NULL`, `active_minutes REAL NULL`, intensity fields |
| `daily_activity` | daily aggregates: `steps_count INTEGER NULL`, `active_minutes`, `energy_kcal`, `distance_m` |
| `body_measurements` | weight_kg, height_m, body_fat_pct, etc. nullable typed columns |
| `hydration_entries` | volume_ml, beverage_code NULL |
| `supplement_entries` | supplement_code, dose, unit |
| `symptom_entries` | symptom_code, severity, notes_redacted_flag |
| `subjective_check_ins` | energy, mood, stress scores on **normalized** scales; `scale_code`; `completed_at` TEXT (UTC); incomplete rows are not engine inputs |
| `laboratory_results` | analyte_code, value_double, unit, ref_low/high NULL, collected_at |
| `manual_entries` | entry_kind, value_double NULL, value_text NULL, unit NULL |

**Canonical units (normative for storage):** steps → integer count; energy/calories → **kcal** (`energy_kcal`); distance → meters; temperature → °C; HR → bpm; HRV → ms; SpO2 → percent; durations → minutes or seconds as column name indicates.

#### Sleep

| Table | Core columns |
|-------|--------------|
| `sleep_sessions` | `is_nap INTEGER`, allows multiple per day / split sleep; `duration_min`, `time_in_bed_min`, `efficiency_pct NULL`; stage minute totals optional summaries |
| `sleep_stage_intervals` | `sleep_session_fact_id` FK → fact_records of session; `stage_code` (`light`,`deep`,`rem`,`awake`,…); `start_utc`, `end_utc` |

#### Workouts and swim

| Table | Core columns |
|-------|--------------|
| `workouts` | sport_type, duration_s, vendor_load NULL (comparison only), avg/max hr NULL |
| `workout_samples` | parent workout fact id; t, hr, zone, pace, power, … |
| `swim_sessions` | `workout_fact_record_id` TEXT FK → fact_records (**required link** to parent workout fact); pool_length_m NULL, total_distance_m, strokes NULL |
| `swim_lengths` | parent swim session fact id; length_index, duration_s, strokes, style_code |

Every `swim_sessions` row **must** reference its parent `workouts` fact via `workout_fact_record_id`.

#### Nutrition

| Table | Core columns |
|-------|--------------|
| `nutrition_meals` | local_health_day, meal_type, source |
| `nutrition_items` | meal_fact_id, food_code/label, quantity, unit |
| `nutrition_nutrients` | item_fact_id or meal_fact_id; nutrient_code; amount; unit |

Nutrient and lab codes resolve through **`vocab_definitions`** (registry), not free EAV for arbitrary vitals.

### `vocab_definitions` (registry only)

| Column | Notes |
|--------|-------|
| `vocab` | `feature_code`, `nutrient`, `lab_analyte`, `symptom`, … |
| `code` | |
| `definition_json` / unit / display_key | enforced definition row required before use |
| Unique | `(vocab, code)` |

Acceptable for derived feature codes and long nutrient/lab vocabularies. **Forbidden** as the primary store for core wearable vitals (use typed tables).

---

## Source policy and selections

### `source_policies`

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `tenant_id` | TEXT NULL | null = system default |
| `name` | TEXT | |
| `status` | TEXT | |
| `effective_from`, `effective_to` | TEXT NULL | dates or timestamps |
| `generation` | INTEGER | source-policy generation for baseline reset |
| `version_n` | INTEGER | |
| `created_at` | TEXT | |

### `source_policy_rules`

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `policy_id` | TEXT FK | |
| `metric_family` | TEXT | see defaults |
| `granularity` | TEXT NULL | optional; set only when the same `metric_family` needs different authoritative providers per grain type |
| `authoritative_provider` | TEXT | `oura`, `google_health`, `polar` |
| `authoritative_data_source_family` | TEXT NULL | e.g. `google-wearables` |
| `params_json` | TEXT NULL | rare overrides; `json_valid` |

**Uniqueness (SQLite-safe partial unique indexes):** SQLite treats each `NULL` as distinct in a plain `UNIQUE` constraint, so null vs non-null `granularity` must be two partial indexes—not a single composite unique including nullable `granularity`:

- `UNIQUE (policy_id, metric_family) WHERE granularity IS NULL`
- `UNIQUE (policy_id, metric_family, granularity) WHERE granularity IS NOT NULL`

**One authoritative source per rule key:** exactly **one** authoritative provider (and optional data-source family) per `(policy_id, metric_family[, granularity if needed])`. **Do not** encode multiple authoritative providers as ranked rows for the same key. Candidate **display order** belongs only on `source_selection_candidates.rank` and is **never** fallback. Missing authoritative source remains `missing_authoritative` until policy override or user override.

Default system policy (proposed):

| Metric family | Authoritative |
|---------------|---------------|
| Sleep sessions and stages | Oura |
| Overnight HRV, RHR, temperature, respiration | Oura |
| Daytime HR, steps, activity, daytime SpO2, daytime temperature, daytime signals | `google_health` + **google-wearables** (Fitbit-origin) |
| Workout HR/sessions, swim, intensity, inputs to internal load | Polar |

### `source_grains` (stable episode identity header)

Provider-independent **stable episode identity** for `session` and `workout` grains. This table is the **header only**—not a versioned membership container. **Do not** use a vendor session/workout id as the selection grain key—that would keep same-episode facts from different providers on separate grains and prevent them from becoming selection candidates for one episode.

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | UUIDv7; **stable canonical grain id**; used as `source_selections.grain_key` for session/workout; unchanged across membership versions |
| `tenant_id` | TEXT | |
| `metric_family` | TEXT | e.g. sleep session, workout session |
| `granularity` | TEXT | `session` or `workout` |
| `created_at` | TEXT | |

Session/workout **`grain_key` values are stable and provider-independent** (`source_grains.id`). A **newly discovered distinct episode** allocates a **new** `source_grains` identity. UUID allocation itself is **not** content-deterministic; identity stability is application-managed once an episode is known.

### `source_grain_versions` (versioned membership/interval container)

One version row per matched membership snapshot for a stable `source_grains` identity. Canonical interval/day, algorithm pin, and current-version flags live **here**, not on the identity header.

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | UUIDv7 for this version row |
| `source_grain_id` | TEXT FK → source_grains | stable episode identity |
| `local_health_day` | TEXT | wake-date / workout local day bucket for this version |
| `grain_start_utc`, `grain_end_utc` | TEXT | canonical matched interval (UTC RFC3339) for this version |
| `match_algorithm_version` | TEXT | pinned episode-matcher version for this version |
| `version_n` | INTEGER | membership container version under the grain |
| `is_current` | INTEGER | 0/1 CHECK |
| `superseded_by` | TEXT NULL | pointer to successor version row when superseded |
| `created_at` | TEXT | |

**Uniqueness:**

- `UNIQUE (source_grain_id, version_n)`
- **One-current partial unique:** one current row per `source_grain_id` WHERE `is_current = 1`

### `source_grain_members`

Membership of candidate facts in a **grain version**. **Real FKs only.**

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `tenant_id` | TEXT | |
| `source_grain_version_id` | TEXT FK → source_grain_versions | parent grain **version** (not the stable identity header) |
| `fact_record_id` | TEXT FK → fact_records | **real FK**; never null |
| `match_algorithm_version` | TEXT | must match parent grain version’s algorithm |
| `match_reason` | TEXT | e.g. `time_overlap`, `explicit_link`, `same_bout` |
| Unique | `(source_grain_version_id, fact_record_id)` | membership unique per version |

**Episode-matching rules:**

1. Same complete input fact set + same `match_algorithm_version` → **identical membership** (deterministic matching of who belongs together). This does **not** mean UUID allocation of `source_grains.id` is content-deterministic.
2. **Late arrivals** create a **new** `source_grain_versions` row (and new membership set) and a new `source_selections` version while **keeping the same** `source_grains.id` / `grain_key`—**never** silently rewrite historical membership or selection rows.
3. A **newly discovered distinct episode** gets a **new** `source_grains` identity (and its first version).
4. Multiple concurrent sleeps/workouts remain **first-class separate** `source_grains` (separate episodes).
5. Vendor session/workout ids may appear on facts for provenance only; they are **not** grain keys.

### `source_selections` (**required** for engine inputs)

Exactly **one versioned decision row** per required non-null **provider-independent `grain_key`**. Replaces unsafe table-name/id pointers and drifting `is_authoritative` flags. **Alternatives are not stored here**—they live only in `source_selection_candidates`.

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `tenant_id` | TEXT | |
| `metric_family` | TEXT | |
| `granularity` | TEXT | `daily_metric`, `interval`, `session`, `workout` |
| `grain_key` | TEXT | **required, non-null**, **provider-independent** grain identity (see below); for session/workout this is the stable `source_grains.id` |
| `source_grain_id` | TEXT NULL FK → source_grains | **required** when `granularity` ∈ {`session`,`workout`}; null for daily/interval; points at the **stable identity header**, not a version row |
| `source_grain_version_id` | TEXT NULL FK → source_grain_versions | **required** when `granularity` ∈ {`session`,`workout`}; **null** for daily/interval; pins the **exact membership snapshot** used for this selection |
| `local_health_day` | TEXT NULL | denormalized for daily queries; required content for daily grains |
| `grain_start_utc`, `grain_end_utc` | TEXT NULL | for interval/session/workout grains (from the pinned grain version when session/workout) |
| `selected_fact_record_id` | TEXT NULL FK → fact_records | **real nullable FK**: non-null when a fact is selected; **null only** when `selection_reason = missing_authoritative` |
| `source_policy_version_id` | TEXT FK | |
| `selection_reason` | TEXT | `policy_match`, `only_source`, `user_override`, `missing_authoritative` |
| `missing_reason` | TEXT NULL | **required** when `selection_reason = missing_authoritative` (e.g. `authoritative_provider_disconnected`, `no_fact_for_grain`); **null** otherwise |
| `version_n` | INTEGER | |
| `is_current` | INTEGER | 0/1 CHECK |
| `superseded_by` | TEXT NULL | prior version pointer |
| `created_at` | TEXT | |

**One-current partial unique key:** one current row per `(tenant_id, metric_family, granularity, grain_key)` WHERE `is_current = 1`.

**`grain_key` construction (non-null, provider-independent):**

| Granularity | `grain_key` form |
|-------------|------------------|
| `daily_metric` | `local_health_day` as `YYYY-MM-DD`; grain identity is **metric_family + date** via the unique key (provider-independent) |
| `interval` | `{start_utc}` + `|` + `{end_utc}` (UTC RFC3339 pair; provider-independent) |
| `session` | stable `source_grains.id` for the episode (**never** vendor session id; **never** a `source_grain_versions.id`) |
| `workout` | stable `source_grains.id` for the episode (**never** vendor workout id; **never** a `source_grain_versions.id`) |

Daily/interval keys are **content-derived**. Session/workout keys are **stable allocated UUIDs** (provider-independent), **not** content-deterministic. Episode-matching **membership** is deterministic for a complete input set + algorithm.

**CHECK / FK invariants:**

- If `selection_reason = 'missing_authoritative'` then `selected_fact_record_id IS NULL` AND `missing_reason IS NOT NULL`.
- If `selection_reason != 'missing_authoritative'` then `selected_fact_record_id IS NOT NULL` AND `missing_reason IS NULL`.
- If `granularity` ∈ {`session`,`workout`} then `source_grain_id IS NOT NULL` AND `source_grain_version_id IS NOT NULL` AND `grain_key = source_grain_id` (stable identity header).
- If `granularity` ∈ {`daily_metric`,`interval`} then `source_grain_id IS NULL` AND `source_grain_version_id IS NULL`.
- **Version belongs to grain:** the row referenced by `source_grain_version_id` must have `source_grain_versions.source_grain_id = source_selections.source_grain_id` (composite FK when the dialect allows, else application invariant enforced in repositories).
- **Selected fact is a member of the pinned version:** when `selected_fact_record_id` is non-null and granularity is session/workout, a `source_grain_members` row must exist for `(source_grain_version_id, selected_fact_record_id)`.

**Rules:**

1. Multiple sleeps/workouts → **separate** canonical grains (separate `source_grains` / `grain_key`s), each with its own current selection.
2. Cross-provider facts for the **same** episode share one pinned `source_grain_versions` membership set and compete as selection candidates under that single stable `grain_key`.
3. **Never average** candidates into a synthetic fact or selection.
4. **Never auto-fallback** to a non-authoritative candidate; missing authoritative → `missing_authoritative` with `missing_reason`.
5. Engine inputs read **current** selections only; candidates are for Why UI, data quality, and future explicit override—not silent substitution.
6. Late-arriving facts create a new `source_grain_versions` membership snapshot and a new selection version (new `source_grain_version_id`) under the **same** `source_grains.id` / `grain_key`; historical versions remain queryable.

### `source_selection_candidates`

Eligible alternative facts for a selection decision. **Default:** members of the selection’s pinned `source_grain_version_id` membership set. **Exception:** explicitly marked **ineligible near-matches** (non-members) may appear with eligibility `ineligible` and a required reason for Why UI. **Never averaged. Never auto-fallback.**

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `tenant_id` | TEXT | |
| `source_selection_id` | TEXT FK → source_selections | parent decision (same version) |
| `fact_record_id` | TEXT FK → fact_records | **real FK**; never null |
| `rank` | INTEGER | **display/eligibility order only**; lower = higher visibility; **never** silent fallback |
| `eligibility` | TEXT | `eligible`, `ineligible` |
| `reason` | TEXT | e.g. `non_authoritative_provider`, `wrong_data_source_family`, `excluded_overlap`, `quality_low`, `near_match_not_member` |
| Unique | `(source_selection_id, fact_record_id)` | |

**Candidate membership invariant:** eligible candidates for session/workout selections **must** be members of the parent selection’s pinned `source_grain_version_id`. Non-members may appear only as `eligibility = ineligible` with an explicit near-match (or other) reason. When a selection is version-superseded, its candidates are historical with that version; new selection versions get new candidate sets. **Only** `rank` (here) orders How/Why candidate display—not policy rules.

---

## Derivation lineage

### `derivation_runs`

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `tenant_id` | TEXT | |
| `artifact_kind` | TEXT | `feature`, `baseline`, `score`, `factor`, `anomaly`, `recommendation` |
| `local_health_day` | TEXT NULL | |
| `formula_version` | TEXT | e.g. `general_recovery_v0.1.0` |
| `source_policy_version_id` | TEXT NULL | |
| `source_policy_generation` | INTEGER NULL | |
| `dependency_hash` | TEXT | |
| `confidence` | REAL NULL | |
| `freshness_at` | TEXT NULL | |
| `as_of_at` | TEXT NULL | UTC RFC3339 evaluation instant used for freshness |
| `status` | TEXT | `ok`, `partial`, `insufficient` |
| `superseded_by` | TEXT NULL | |
| `created_at` | TEXT | |

### `derivation_inputs`

**No polymorphic `input_kind` / `input_id`.** Each input row uses **nullable typed FK columns** plus `role`. Exactly one typed FK is non-null (SQL CHECK). No table-name/id pointer anywhere.

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `derivation_run_id` | TEXT FK | |
| `tenant_id` | TEXT | |
| `role` | TEXT | e.g. `hrv`, `sleep_duration`, `prior_load_balance` |
| `source_selection_id` | TEXT NULL FK → source_selections | |
| `fact_record_id` | TEXT NULL FK → fact_records | |
| `daily_health_feature_id` | TEXT NULL FK → daily_health_features | |
| `baseline_id` | TEXT NULL FK → baselines | |
| `daily_health_score_id` | TEXT NULL FK → daily_health_scores | |
| `anomaly_id` | TEXT NULL FK → anomalies | |
| `recommendation_id` | TEXT NULL FK → recommendations | |

**CHECK (exactly one typed input set):**

```sql
CHECK (
  (CASE WHEN source_selection_id IS NOT NULL THEN 1 ELSE 0 END
 + CASE WHEN fact_record_id IS NOT NULL THEN 1 ELSE 0 END
 + CASE WHEN daily_health_feature_id IS NOT NULL THEN 1 ELSE 0 END
 + CASE WHEN baseline_id IS NOT NULL THEN 1 ELSE 0 END
 + CASE WHEN daily_health_score_id IS NOT NULL THEN 1 ELSE 0 END
 + CASE WHEN anomaly_id IS NOT NULL THEN 1 ELSE 0 END
 + CASE WHEN recommendation_id IS NOT NULL THEN 1 ELSE 0 END) = 1
)
```

Every feature, baseline, score, factor, anomaly, and recommendation points at a `derivation_run_id`.

---

## Engine outputs

### `daily_health_features`

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `tenant_id` | TEXT | |
| `local_health_day` | TEXT | |
| `feature_code` | TEXT | registry-enforced |
| `value_double` | REAL NULL | prefer double |
| `value_json` | TEXT NULL | multi-part only; `json_valid` |
| `unit` | TEXT NULL | |
| `derivation_run_id` | TEXT FK | |
| `formula_version` | TEXT | |
| `dependency_hash` | TEXT | |
| `version_n`, `is_current` | | |

### `baselines`

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `tenant_id` | TEXT | |
| `feature_code` | TEXT | |
| `context_code` | TEXT | stratification key |
| `as_of_day` | TEXT | local date baseline applies to |
| `window_days` | INTEGER | |
| `sample_count` | INTEGER | |
| `center` | REAL | median |
| `mad` | REAL NULL | median absolute deviation (unscaled) |
| `robust_scale` | REAL | **σ-equivalent scale used in z**: see health-engine (prefer `1.4826 * MAD`) |
| `p25`, `p75` | REAL NULL | |
| `ewma` | REAL NULL | α pinned in formula version |
| `fallback_dispersion_used` | INTEGER | 0/1; set when MAD path unused |
| `maturity` | TEXT | `insufficient`, `min`, `mature` |
| `derivation_run_id` | TEXT FK | |
| `formula_version` | TEXT | |
| `dependency_hash` | TEXT | |
| versioning columns | | |

### `daily_health_scores`

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `tenant_id` | TEXT | |
| `local_health_day` | TEXT | |
| `score_code` | TEXT | **required** registry: `recovery` (v0.1.0 only shippable score); `sleep`, `strain`, `activity`, `readiness` reserved until accepted formulas |
| `status` | TEXT | `ok`, `insufficient`, `partial` |
| `score` | INTEGER NULL | 0–100; null if insufficient |
| `available_weight` | REAL NULL | sum of present component weights with disclosed coverage |
| `confidence` | REAL | 0–1 |
| `formula_version` | TEXT | recovery ships as `general_recovery_v0.1.0` when that formula is used |
| `dependency_hash` | TEXT | |
| `freshness_at` | TEXT NULL | |
| `as_of_at` | TEXT NULL | evaluation instant for freshness |
| `derivation_run_id` | TEXT FK | |
| versioning | | Unique current `(tenant_id, local_health_day, score_code)` |

**Ship rule:** only score codes with **accepted formula specs and golden fixtures** may be written by the engine. `general_recovery_v0.1.0` is the executable unvalidated recovery formula. Other `score_code` values **must not ship** until their formula specs and golden fixtures are accepted—unspecified weights are not implementable.

### `score_factors`

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `daily_health_score_id` | TEXT FK → daily_health_scores | |
| `tenant_id` | TEXT | |
| `factor_code` | TEXT | |
| `sign` | INTEGER | -1, 0, +1 |
| `magnitude` | REAL | |
| `weight` | REAL NULL | component weight used |
| `present` | INTEGER | 0/1 |
| `display_label_key` | TEXT | i18n key |
| `derivation_run_id` | TEXT FK | |

### `anomalies`

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `tenant_id` | TEXT | |
| `feature_code` | TEXT | |
| `started_on`, `ended_on` | TEXT NULL | local dates |
| `severity` | TEXT | |
| `z_like` | REAL NULL | |
| `formula_version` | TEXT | |
| `is_active` | INTEGER | |
| `derivation_run_id` | TEXT FK | |
| versioning | | |

### `recommendations`

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `tenant_id` | TEXT | |
| `local_health_day` | TEXT | |
| `role` | TEXT | `primary`, `supporting` |
| `rule_id` | TEXT | |
| `priority` | INTEGER | |
| `conflict_group` | TEXT | |
| `title_key`, `body_key` | TEXT | deterministic copy keys |
| `params_json` | TEXT NULL | `json_valid` |
| `suppressed_by` | TEXT NULL | |
| `ruleset_version` | TEXT | |
| `training_label` | TEXT NULL | `hard`, `moderate`, `light`, `rest`, `insufficient` |
| `derivation_run_id` | TEXT FK | |

---

## Preferences

### `user_preferences`

Typed columns preferred: theme, units, notification toggles, model enablement, **`sleep_target_min`** (explicit preference; provisional default **480** applied in engine when null—never chronically short personal median as target).

---

## Model layer, notifications, health experiments, feature flags

Core product tables do not require model configuration. Model tables are used only when the optional agent path is enabled.

### `model_connections` / `model_provider_configs` / `model_task_selections`

- Multiple connected providers; capabilities snapshot; default model; **per-task** selection or **disable**.
- User API keys: envelope-encrypted (`ciphertext` BLOB + `key_version`).
- No silent fallback metadata: selected provider/model is explicit per run.
- Local endpoint URLs subject to outbound allowlist (SSRF controls in app layer).

### `conversations`, `messages`, `conversation_events`

- SSE-oriented storage: monotonic **`event_id`**, **`run_id`**, tenant-scoped rows for replay.
- Canonical conversation history is provider-agnostic so models can switch without rewrite.
- No score invention fields; scores only via tools/context.

### `tool_calls`, `tool_results`

Typed tool name, canonical args hash, result ref, status; link to conversation message / run.

### `confirmations`

One-time, expiring confirmations for mutating tools, bound to tenant, user, run, tool, canonical args hash, and idempotency key. Model cannot confirm.

### `egress_manifests`, `model_egress_consents`

- Consent and persisted context manifest are **provider / model / purpose / data-scope** specific.
- Manifest records what left the trust boundary (redacted structure); granted/revoked timestamps.
- Review provider no-training/data-use policy before enable (process control; not a schema claim).

### `health_experiments` (first-class product entity)

Personal **observational** health experiments. **Not** feature flags. **Not** causal inference claims.

| Column | Physical type | Notes |
|--------|---------------|-------|
| `id` | TEXT PK | |
| `tenant_id` | TEXT | |
| `hypothesis` | TEXT | free-text hypothesis (sensitive; user-authored) |
| `protocol` | TEXT | what the user plans to do / observe |
| `started_on`, `ended_on` | TEXT NULL | local dates |
| `status` | TEXT | `planned`, `active`, `completed`, `abandoned` |
| `outcome_feature_codes_json` | TEXT | JSON array of feature codes watched; `json_valid` |
| `confounder_notes` | TEXT NULL | known confounders; observational caveat |
| `design_class` | TEXT | fixed: **`observational_non_causal`** for v0 (no causal claim storage) |
| `created_at`, `updated_at` | TEXT | |

Engine and UI must present health experiments as **observational / non-causal**. Do not conflate with product `feature_flags`.

### `feature_flags`

Product/experiment **flag** assignments for rollout (non-health payload keys). Distinct from `health_experiments`.

### `notifications`

User-visible notification records; PHI-minimized body keys.

---

## Optional vector boundary (phase four / future agent only)

**Not MVP schema.** Deterministic SQL remains source of truth for all scores, recommendations, dashboard, and export **without** embeddings or models. Vector retrieval is optional later functionality only. Turso native vector columns/indexes are an **implementation option** for production.

When introduced, store **tenant-scoped** embeddings of approved **derived summaries**, **user-authored** journal/conversation content, and **curated knowledge**. **Do not** embed canonical **raw measurements** by default.

### Required parent/child shape (no polymorphic source pointer)

**Forbidden:** generic `source_kind` + `source_id` (or table-name/id) pointers.

#### `retrieval_documents` (parent)

| Column | Notes |
|--------|-------|
| `id` | TEXT PK UUIDv7 |
| `tenant_id` | **required**; mandatory predicate on every retrieval |
| `document_class` | `derived_summary`, `journal`, `conversation`, `curated_knowledge` |
| `source_content_hash` | hash of embedded text/bytes |
| `sensitivity` | classification |
| `consent_id` | required consent lineage |
| `created_at` | UTC RFC3339 |
| Typed source FKs | nullable typed links with **CHECK exactly one** (or an explicit multi-link child table)—e.g. `conversation_message_id`, `journal_entry_id`, `derived_summary_id`, `curated_knowledge_id` |
| `deletion_lineage` | links to delete/scrub for restore-safe removal |

If typed source FKs are not yet fully specified for a document class, **do not implement** that class until typed links exist. No unsafe table-name/id pointer as a temporary substitute.

#### `vector_embeddings` (child; real FK)

| Column | Notes |
|--------|-------|
| `id` | TEXT PK |
| `tenant_id` | required; must match parent |
| `retrieval_document_id` | TEXT **real FK** → retrieval_documents |
| `embedding` | vector payload (e.g. optional Turso `F32_BLOB`) |
| `embedding_model` | model id |
| `embedding_provider` | provider id |
| `embedding_version` | version string; **rebuild** on change |
| `dimension` | INTEGER |
| `created_at` | UTC RFC3339 |

Rules:

1. **Tenant predicate mandatory** on every retrieval path (filter before/with ANN).
2. Evaluate **filtered ANN** under tenant predicates in a spike.
3. **Rebuild** embeddings when `embedding_version` changes; retain delete/rebuild lineage.
4. **Delete embeddings with source data** (privacy delete and source deletion); cascade via `retrieval_document_id`.
5. Local relational tests may use SQLite; **vector integration tests use libSQL/Turso**.

See [ADR 0003](../adr/0003-libsql-operational-store.md) and [api-tools-and-agent.md](api-tools-and-agent.md).

---

## Jobs, idempotency, audit

### `jobs`, `job_leases`, `job_attempts`, `job_dead_letters`

See [repository-and-services.md](repository-and-services.md). Key fields: `tenant_id`, `job_type`, `payload_json` (TEXT `json_valid`), `status`, `priority`, `run_after`, `attempts`, `max_attempts`, `idempotency_key`, `fence_token`, `last_error_class`.

### `idempotency_keys`

| Column | Notes |
|--------|-------|
| `tenant_id`, `key` | unique pair |
| `request_hash`, `response_code`, `response_body_ref` | |
| `created_at`, `expires_at` | TEXT UTC |

### `audit_events`

| Column | Notes |
|--------|-------|
| `tenant_id` NULL for system | |
| `actor_type`, `actor_id` | user, system, worker |
| `action` | `connection.create`, `export`, `delete`, `tool.invoke`, … |
| `resource_type`, `resource_id` | |
| `metadata_json` | **no health values** |
| `created_at` | TEXT UTC |

### `data_quality_findings`

| Column | Notes |
|--------|-------|
| `tenant_id`, `local_health_day` NULL | |
| `code`, `severity`, `provider` NULL | |
| `message_key`, `context_json` | no raw PHI dumps |
| `resolved_at` NULL | |

---

## Exports and deletions

### `export_requests`

Status, format (`json`, `zip`), pointer into **private encrypted object storage**, expires_at, idempotency. Download URLs time-limited; cache/no-store controls on private objects.

### `deletion_requests`

Status pipeline: `requested` → `jobs_cancelled` → `rows_scrubbed` → `backups_scheduled` → `completed`.

### Deletion artifacts (two separate stores)

Privacy deletion **hard-deletes / hard-scrubs** user-linked health data, conversations, egress manifests, **vector embeddings**, and export artifacts; revokes credentials. Vendor tombstones are unrelated and only apply to vendor-side deletes until scrub.

There are **two distinct artifacts**. Do **not** claim a single “non-linkable yet fully replayable forever” ledger. Do **not** claim permanent tenant-lifetime non-linkability.

#### 1. Minimal completion / audit proof (`deletion_completion_proofs`)

| Property | Rule |
|----------|------|
| Purpose | Prove a privacy deletion completed for operators/compliance audit |
| Contents | Request id (or opaque completion id), completion timestamp, status `completed`, high-level scrub class counts **without** health values, **without** email/display name, **without** raw tenant UUIDs when avoidable (prefer opaque completion id) |
| Linkability | **Minimal non-identifying**; not sufficient to reconstruct health data; not a restore map |
| Retention | Align with audit retention policy (see Retention); not a restoration-suppression mechanism |

#### 2. Restoration-suppression ledger (`restoration_suppression_ledger`)

| Property | Rule |
|----------|------|
| Purpose | After backup restore, **suppress re-serving** data that was privacy-deleted before restore |
| Contents | **Only** HMAC-derived selectors under a **dedicated deletion key** (KEK/DEK separate from general DB encryption): e.g. `HMAC(deletion_key, "tenant:" \|\| tenant_id)`, `HMAC(deletion_key, "object:" \|\| object_class \|\| ":" \|\| object_id)`—**no** identity values, **no** health values, **no** raw emails/names |
| Access | **Access-separated** from primary app DB credentials and from completion-proof storage; operators with app access do not automatically hold the deletion key |
| Replay | **Replayed before restored data is served** (restore runbook: restore → load suppression ledger → block/suppress matching selectors → then open traffic) |
| Retention | Retain until **all relevant backups that could resurrect the data expire**, plus a **30-day safety margin**, then **destroy** the ledger entries and rotate/retire the deletion key material for those selectors |
| Non-claim | This is **not** permanent tenant-lifetime non-linkability; HMAC selectors under a live key are linkable to operators who hold the deletion key. After key destruction and ledger purge, residual linkability is bounded by cryptographic key erasure assumptions |

#### Restore sequence (normative)

1. Restore backup into isolated environment.
2. Load `restoration_suppression_ledger` (access-separated).
3. Apply suppression (hard-delete or hard-block matching selectors) **before** any user-facing serve.
4. Verify deleted tenants/objects are not servable.
5. Only then promote / serve.

See [security.md](security.md), [operations.md](operations.md), [../testing.md](../testing.md).

---

## Tenant composite FKs and indexes

- Prefer composite unique keys: `(tenant_id, … natural key …)`.
- Child tables carry `tenant_id` and match parent `(tenant_id, id)` where the dialect allows composite FKs; if SQLite single-column FK to `id` is used, **application repositories always constrain `tenant_id`** identically.
- Hot read indexes: current facts by day, selections by `(tenant_id, metric_family, granularity, grain_key)` WHERE current, scores by `(tenant_id, local_health_day, score_code, is_current)`, raw by `(tenant_id, provider, stream, vendor_record_id)`, `raw_payload(content_hash)` non-unique.

---

## Revision model (shared pattern)

For versioned domain tables (`fact_records`, selections, features, baselines, scores, …):

1. Insert new row with `version_n = prev+1`, `is_current=1`.
2. Set previous `is_current=0`, `superseded_at`, `superseded_by`.
3. Partial unique index / enforced uniqueness: one current row per natural key.
4. Reads default to `is_current=1`; history APIs read all versions.

Derived rows always reference `derivation_run_id` and store `dependency_hash`.

---

## Migrations

- **Tool:** Alembic, single linear chain for MVP.
- **Dev:** `alembic upgrade head` against local libSQL/SQLite with `foreign_keys=ON`.
- **Prod:** expand/contract for breaking schema changes.
- **Schema vs formula migration:** table changes use Alembic; formula/policy changes use new `formula_version` / policy generation and recompute jobs—**do not** rewrite historical score rows in place.
- **Phase zero:** validate exact Python + SQLAlchemy 2 + Alembic path on Turso, concurrency, encryption assumptions, volume, and later-ready vector options—not a generic store go/no-go ([../roadmap.md](../roadmap.md), [../adr/0003-libsql-operational-store.md](../adr/0003-libsql-operational-store.md)). Only a proven blocker reopens the ADR.
- Support **N / N−1** rolling expand/contract deploys for breaking schema changes.

---

## Retention

| Class | Proposed default |
|-------|------------------|
| Raw payloads / revisions | Tenant lifetime until privacy hard-scrub |
| Facts / scores / derivation runs | Tenant lifetime until privacy hard-scrub |
| Vendor tombstones | Until privacy scrub or object fully scrubbed |
| Privacy tombstones | Transient during deletion pipeline only |
| Job attempts | 90 days |
| Audit events | 2 years metadata-only |
| Export artifacts | 7 days after create |
| Dead letters | 30 days then purge payload |
| Deletion completion proofs | Align with audit events (metadata-only) |
| Restoration-suppression ledger | Until all relevant backups expire **+ 30 days**, then destroy |
| OAuth states | Expiry + short purge |

---

## Related

- [ingestion-and-sync.md](ingestion-and-sync.md)
- [health-engine.md](health-engine.md)
- [../adr/0004-versioned-provenance.md](../adr/0004-versioned-provenance.md)
- [../adr/0005-authoritative-source-policy.md](../adr/0005-authoritative-source-policy.md)
