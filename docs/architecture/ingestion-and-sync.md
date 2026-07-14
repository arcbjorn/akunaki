# Ingestion and sync

**Status:** Proposed

**Last reviewed:** 2026-07-13

Authoritative for **connector interface**, **sync**, and co-authoritative for **raw/normalized models** and **source priority** (coverage matrix items 4, 5, 6, 7).

No connectors are implemented in this repository.

---

## Correcting connector ambiguity (July 2026)

| Source | Role in this architecture |
|--------|---------------------------|
| **Oura** | MVP **server connector** via Oura Cloud API V2 (OAuth + documented webhook signatures) |
| **Google Health** (`google_health`) | MVP **server connector** via **Google Health API v4**, the cloud successor to the legacy Fitbit Web API. Google OAuth; restricted scopes and security review; default intraday list data; signed auto webhooks and provenance. Select the **google-wearables** data-source family for Fitbit daytime policy; preserve DataSource origin/device. UI may say **Fitbit via Google Health**. |
| **Polar** | MVP **server connector** via **Polar AccessLink** (documented webhook signatures where offered; poll-first MVP) |
| **Legacy Fitbit Web API** | **Not** an MVP connector. Stops syncing in **September 2026**. Do not design new integration against it. |
| **Android Health Connect** | **Not** a server connector. On-device only. Future **Android companion bridge**—not MVP. Distinct from Apple Health/HealthKit. |
| **Apple Health / HealthKit** | **Not** a server connector. Device-local; fine-grained user-authorized. Future **native iOS bridge** that syncs typed, provenance-preserving records to the backend. **No** native mobile app in MVP. Distinct from Android Health Connect. |
| **Google Fit client APIs** | **Not a foundation**. Must not underpin any connector |

Provider ids in storage and code (MVP): `oura`, `google_health`, `polar`.

### Fitbit-origin device naming (open validation)

The exact consumer device marketing name often referred to informally as **Fitbit Air** and the precise Google Health / google-wearables capability surface for that hardware are an **open validation item**. Until a phase-zero spike confirms device ↔ field coverage, docs must not claim complete metric parity for that device. Design assumes Google Health resources available under approved scopes for Fitbit-origin daytime streams, not device-proprietary SDKs.

### Polar swimming fields (open validation)

Polar AccessLink has **v3 and v4** surfaces. Whether **Verity Sense** swimming fields required for MVP load/swim analytics are available on v3, v4, or neither for our app tier is a **phase-zero capability test**. See [../roadmap.md](../roadmap.md).

---

## Provider capability matrix (proposed targets)

| Capability | Oura | Google Health (google-wearables) | Polar AccessLink | Health Connect (future) | Apple Health / HealthKit (future) |
|------------|------|----------------------------------|------------------|-------------------------|-----------------------------------|
| OAuth server-side | Yes (V2) | Yes (Google OAuth; restricted scopes / security review) | Yes | N/A (on-device) | N/A (device-local; user-authorized HealthKit) |
| Webhooks | Documented signature; preferred trigger | Signed auto webhooks + endpoint authorization | Provider-documented signature; poll-first MVP | N/A | N/A |
| Sleep sessions | Strong | Available; not default overnight authority | Limited vs Oura | Future companion | Future iOS bridge |
| Overnight HRV / RHR / temp / respiration | Strong | Partial | Partial | Future companion | Future iOS bridge |
| Daytime HR / intraday | Limited vs google-wearables | Strong (default list / intraday under scopes) | Workout-centric | Future companion | Future iOS bridge |
| Steps / activity | Partial | Strong (Fitbit-origin daytime policy) | Partial | Future companion | Future iOS bridge |
| SpO2 / daytime temperature signals | Possible | Daytime/spot when available to app | Device-dependent | Future companion | Future iOS bridge |
| Workout intervals | Basic | Activity sessions (alternatives; exclude from load when Polar overlaps) | **Strong** | Future companion | Future iOS bridge |
| Swimming structure | Weak | Weak | **Target strong** (validate Verity Sense) | Future companion | Future iOS bridge |
| Training load | Vendor scores comparison only | Vendor scores comparison only | **Canonical load** from HR zones (internal) | Future companion | Future iOS bridge |
| Provenance / DataSource | Vendor ids | Preserve DataSource origin/device | Vendor device metadata | Future companion | Future bridge preserves typed HealthKit provenance |

### Honest access gates

| Gate | Impact |
|------|--------|
| Google Health security review + restricted scopes | Without approved scopes, daytime HR/intraday resolution degrades; engine may mark daytime metrics `insufficient` or low confidence |
| Oura workspace / webhook registration | Without webhooks, fall back to scheduled poll only |
| Polar rate limits and AccessLink registration | Controls backfill speed |
| User denies a scope | Connection `active` with reduced `scopes_granted`; quality findings emitted |
| Health Connect | Requires Android companion app; not unblocked by server work alone |
| Apple Health / HealthKit | Requires future native iOS bridge; fine-grained user authorization; not a server connector; no native app in MVP |
| Legacy Fitbit Web API | Do not depend on it for MVP continuity past September 2026 |

---

## Typed connector port

Proposed Protocol (conceptual):

```text
ConnectorPort
  provider_id: Literal["oura","google_health","polar"]
  oauth: OAuthAdapter
  verify_webhook(headers, body, endpoint_auth) -> WebhookEvent | reject
  list_streams() -> list[StreamDescriptor]
  fetch(stream, cursor, window) -> FetchResult
  health_probe(connection_secrets) -> ConnectionProbeResult
```

### `RawEnvelope` (conceptual fields)

| Field | Type | Purpose |
|-------|------|---------|
| `provider` | enum | `oura` / `google_health` / `polar` |
| `stream` | str | |
| `provider_object_key` | str | stable identity when known |
| `fetched_at` | UTC RFC3339 | |
| `received_at` | UTC RFC3339 | ingest time |
| `schema_version` | str | |
| `content_type` | str | |
| `payload` | JSON or bytes | **exact** vendor body |
| `content_hash` | str | sha256 of body |
| `http_status` | int | |
| `request_meta` | redacted dict | no secrets |
| `rate_limit` | optional remaining/reset | |
| `next_cursor` | optional | |
| `data_source` | optional | Google Health DataSource origin/device family |

Normalizers accept `RawEnvelope` (+ connection context) and emit typed fact candidates. Normalizers are pure with respect to clock: timestamps come from envelope and payload, not `now()`, except for `received_at` already on the envelope.

---

## OAuth

| Provider | Proposed notes |
|----------|----------------|
| Oura | OAuth2 authorization code; V2 API; store refresh tokens envelope-encrypted in `connection_secrets` |
| Google Health | **Google OAuth 2.0**; restricted scopes; security review gate; store tokens envelope-encrypted; re-consent required for migration from any legacy Fitbit path |
| Polar | AccessLink OAuth; store tokens; respect token lifetimes |

Common flow:

1. API creates `connections` row `pending`, persists `oauth_states` with **`state_hash`**, **envelope-encrypted PKCE verifier**, **exact redirect URI**, `expires_at` (single-use).
2. User returns with code; API validates hashed state, expiry, unconsumed; decrypts verifier; checks exact redirect; exchanges tokens; encrypts into `connection_secrets` (tokens are **not** stored as plaintext columns on `connections`); marks state `consumed_at`.
3. Enqueue `connection.initial_sync` with idempotency key.
4. On refresh failure with invalid_grant: `needs_reauth` + user-visible finding.

---

## Webhook verification (provider-specific)

**Never claim a generic HMAC scheme for all providers.** Each connector implements the vendor's documented verification.

| Provider | Policy |
|----------|--------|
| **Google Health** | Verify **rotating public-key signature** on the payload **and** endpoint authorization (shared secret / configured auth as documented). Reject on failure. |
| **Oura** | Verify signature **per Oura webhook docs** (documented scheme and headers); enforce timestamp skew / replay protection as documented. |
| **Polar** | Verify signature **per Polar docs** when webhooks are enabled; MVP may be poll-first until registration is complete. |

### Ingress pipeline

1. Identify provider from route / registration; run **that** provider's verifier.
2. Map to `tenant_id` + `connection_id` without trusting body alone for authz.
3. Persist durable **`webhook_inbox`** row with dedupe key (provider delivery id / content hash) and **`body_payload_id` null**; if duplicate, ack and stop.
4. Optionally capture body into **`raw_payload`**: insert payload with **`sync_run_id` null** and `transport_kind = webhook_capture` (source marker); then set `webhook_inbox.body_payload_id`. **One-way FK only** (`body_payload_id` → payload); reverse lookup is through inbox. **Retain every response**; `content_hash` is indexed, not a uniqueness constraint that erases repeats.
5. Return **2xx quickly** (acknowledge).
6. Worker enqueues **refetch** of affected streams/windows (do not perform heavy fetch inline on the request path); refetch creates `sync_runs` and additional `raw_payload` pages with `sync_run_id` set.
7. **Scheduled reconciliation** periodically re-fetches windows to cover missed or failed deliveries.

---

## Sync model

### Modes

| Mode | When |
|------|------|
| **Initial backfill** | New connection; configurable lookback (e.g. 90 days) with overlap safety |
| **Incremental** | Cursor-based or time-window; scheduled |
| **Webhook-triggered** | Narrow window around event after inbox ack |
| **Manual refresh** | User-initiated; rate-limited |
| **Scheduled reconciliation** | Gap fill independent of webhooks |
| **Re-normalize** | Normalizer version bump; reuse raw payloads/revisions |
| **Recompute** | Policy or formula version bump; reuse canonical inputs |

### Transport vs logical records

Separate **exact vendor transport** from **logical records**:

| Layer | Tables (see [data-model.md](data-model.md)) |
|-------|-----------------------------------------------|
| Transport | `sync_runs`, `raw_payload` pages (exact body + redacted metadata; **every** response retained) |
| Logical raw | `raw_objects`, immutable append-only `raw_revisions` (vendor record id, observed/effective/received timestamps, content hash, **schema_version**, deletion state—**no** `normalizer_version` on raw) linked to payload page + optional sync run |
| Progress | `sync_cursors` |
| Webhook | `webhook_inbox` |
| Facts | `fact_records` + typed detail tables (`normalizer_version` on facts) |

### Atomic commit after fetch (crash replay)

After a successful fetch page (or multi-page transaction boundary defined per connector):

1. In **one transaction**: write **new** `raw_payload` page(s) always (same hash still inserts a transport row); append `raw_objects` / `raw_revisions` only when logical content is new; advance `sync_cursors`; insert normalization **outbox** rows (or jobs) for new revision ids; mark `sync_run` progress.
2. Commit.
3. If the process crashes **before** commit: cursors unchanged → safe retry of the same window; logical content-hash check prevents duplicate **revisions** (transport rows may still be re-written on retry after commit—application may also insert transport on each attempt that commits).
4. If the process crashes **after** commit but before normalize completes: outbox/jobs remain → normalize retries idempotently by `raw_revision_id`.

### Overlap windows

Each incremental fetch uses `window_start = cursor - overlap` (36h for sleep, 2h for intraday) to absorb late vendor finalization. Logical revision skip when `content_hash` already present for that object; transport pages always retained.

### At-least-once and idempotent ingestion

1. Fetch may be retried → **new** `raw_payload` row (retain every response); same logical `content_hash` → **no new** `raw_revisions` (or no-op).
2. New logical hash → append `raw_revisions`, update current pointer.
3. Normalize job keyed by `raw_revision_id`; set `normalizer_version` on produced **facts**, not on immutable raw revisions.
4. Fact write creates new fact version if values or lineage change; never update in place.
5. Affected `local_health_day`s enqueued for recompute with idempotency `(tenant, day, formula_version, dependency_hash)`.

### Vendor deletions

Vendor delete notifications produce a **vendor tombstone** raw revision (payload may be empty). Downstream facts for that object are version-superseded with deletion state retained for audit until privacy scrub. Do not silently omit history.

### Rate-limit handling

- Surface vendor 429 into job error class `rate_limit`.
- Honor `Retry-After` when present.
- Per-provider token bucket in worker process (in-memory MVP) coordinated with `connection_health.rate_limit_reset_at`.

### Retry policy

Aligned with [repository-and-services.md](repository-and-services.md): exponential backoff, dead letters, auth failures flip connection health.

### Connection health

Updated on every terminal sync attempt: success timestamp, error class (no bodies), consecutive failures, scope degradation flags, last verified webhook time.

---

## Normalization requirements

Normalizers **must** preserve or derive:

| Field | Rule |
|-------|------|
| UTC instant | Always store true instant (RFC3339 TEXT) |
| Source offset | Preserve when vendor provides |
| IANA timezone | From tenant preference, vendor, or explicit mapping; record source of truth used |
| Local health day | Bucket in chosen IANA zone (`YYYY-MM-DD` TEXT) |
| Wake-date sleep assignment | Sleep bout assigned to **local date of wake**, not onset |
| Units | Convert to canonical units; keep source unit in lineage meta if needed |
| Quality / confidence | From vendor flags + heuristic (missing stages → lower) |
| Freshness | Last confirmation time |
| Device / origin / method | When known; Google Health: preserve DataSource origin/device |
| Raw lineage | `raw_revision_id` (+ payload id) |
| Schema + normalizer version | Required on every fact |
| Vendor record id | Stable vendor id when present |
| Deletion state | Active vs vendor-deleted vs privacy-scrubbed |

### Immutable raw revisions and tombstones

- Never update payload body in place; raw revisions are **immutable**.
- **No `normalizer_version`** on `raw_revisions` (belongs on facts / normalization runs).
- Tombstone reasons: **`vendor_deleted`** or **`privacy_delete` only**—not `superseded`.
- Vendor deletion → vendor tombstone revision (durable until privacy scrub).
- Privacy erase → hard-scrub user-linked rows; privacy tombstones are **transient** pipeline markers only. See [data-model.md](data-model.md).

---

## Authoritative source policy (runtime)

After normalization, episode matching builds provider-independent **`source_grains`** identity headers and current **`source_grain_versions`** (pinned `match_algorithm_version`, canonical interval/day) with **`source_grain_members`** real FKs on **`source_grain_version_id`** for session/workout grains. Then **`source_selections`** produce canonical inputs for the engine: **exactly one current versioned decision per non-null provider-independent `grain_key`** (session/workout keys are stable `source_grains.id`, never vendor session/workout ids and never version-row ids). Session/workout selections **require** both `source_grain_id` and **`source_grain_version_id`** (real FK pinning the exact membership snapshot; the version row must belong to that stable grain). Daily/interval leave both null. When a fact is selected, it **must be a member** of the pinned version. `selected_fact_record_id` is a real **nullable** FK (null only for `missing_authoritative` with required `missing_reason`). Granularity ∈ {`daily_metric`, `interval`, `session`, `workout`}. Daily grains remain metric + date. Alternatives live only in **`source_selection_candidates`** (real `fact_record_id` FK, **`rank`**, eligibility/reason)—never averaged, never auto-fallback. Candidate **`rank`** is display order only, never fallback. Eligible candidates are members of the pinned version unless explicitly listed as ineligible near-matches with reason. Multiple sleeps and workouts per day are separate grains; cross-provider facts for the same episode share one stable grain and compete as candidates under the pinned membership version.

### Default effective contextual mapping

| Metric family | Authoritative | Notes |
|---------------|---------------|-------|
| Sleep sessions and sleep stages | Oura | Candidates retained for Why |
| Overnight HRV, overnight RHR, overnight temperature, overnight respiration | Oura | |
| Daytime HR, steps, activity, daytime SpO2, daytime temperature, other daytime signals | `google_health` with **google-wearables** Fitbit-origin DataSource family | UI may label Fitbit via Google Health |
| Workout HR, workout sessions, swim sessions/lengths, intensity, inputs to **internally computed load** | Polar | Canonical load is always internal from Polar HR zones; swim linked to workout |

### Hard rules

1. **Keep alternatives** in `source_selection_candidates` and expose via data-quality / Why UI.
2. **Never average** conflicting values for the same metric family/`grain_key`.
3. **Do not silently fall back** if authoritative source missing; write `missing_authoritative` + `missing_reason`; engine returns `insufficient` or partial with explicit reason.
4. **One authoritative source rule** per `(policy_id, metric_family[, granularity if needed])`; candidate **`rank`** is display/eligibility order only—not auto-fallback.
5. **Exclude overlapping Google/Fitbit-origin workout samples** from workout load calculations when a Polar workout covers the interval (`exclude_from_load=1` on those samples).
6. Policies are **effective-dated and versioned**; derivation runs pin `source_policy_version_id` / generation.
7. No `is_authoritative` flag that drifts on fact rows—selection is only via `source_selections`.
8. Session/workout grain keys are **stable, provider-independent** `source_grains` ids (not content-deterministic UUIDs; matching determinism is identical membership for the same complete input set + algorithm). Late arrivals create a new `source_grain_versions` membership snapshot and selection version under the same grain id without silently rewriting history; a newly discovered distinct episode gets a new `source_grains` identity.

User overrides (future): tenant policy row with higher precedence than system default, still versioned.

ADR: [../adr/0005-authoritative-source-policy.md](../adr/0005-authoritative-source-policy.md).

---

## Affected-date recomputation

A sync result yields a set of `local_health_day` values (and possibly multi-day baseline windows).

| Change | Recompute scope |
|--------|-----------------|
| Single sleep night | That wake-date; baselines that include it; scores for that day and days whose windows slide |
| Intraday HR partial day | That local day |
| Workout spanning midnight | Both local days for load; ACWR window tail |
| Policy version change | All days from `effective_from` or user-selected range |
| Formula version change | Explicit reprocess job range |

Baselines use rolling windows (see [health-engine.md](health-engine.md)); changing day D invalidates baselines for days whose window includes D, cascading to scores—bounded by max window length (e.g. 42 days) to cap fan-out.

---

## Stream inventory (MVP target)

### Oura

- Sleep, daily sleep/readiness-related summaries, HRV/overnight signals available to V2
- Webhook-triggered fetch of affected resources (documented signature)
- Scheduled full reconcile daily

### Google Health (`google_health`)

- Daytime HR, steps, activity, SpO2, temperature, and related daytime signals via **google-wearables** / Fitbit-origin DataSources under approved scopes
- Default list / intraday-style series as available after security review
- Signed auto webhooks → inbox → refetch; scheduled reconciliation for gaps
- Preserve DataSource origin/device on facts
- Poll path always available

### Polar

- Exercises, samples, heart rate zones, fields available to app for internal load
- Swimming lengths/fields **subject to v3/v4 Verity Sense validation**
- Poll-first sync; webhooks if documented and registered

---

## Related

- [data-model.md](data-model.md)
- [health-engine.md](health-engine.md)
- [repository-and-services.md](repository-and-services.md)
- [../references.md](../references.md)
