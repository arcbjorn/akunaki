# Akunaki 飽くなき backend (Phase Zero foundation)

Model-free **FastAPI + SQLAlchemy 2 + sqlalchemy-libsql + Alembic** foundation.

This package intentionally includes **no** frontend and **no** model/AI SDKs. The authenticated `/v1` product surface ships (see below); the full product schema remains **pending**.

Implemented: the **local** atomic durable-job repository lifecycle (fenced claims with attempt history; transactional completion, retry scheduling, dead-lettering, and lease expiry), the **worker runtime** with retry/backoff policy, **idempotent enqueue**, **envelope encryption** for secret columns, the **OAuth state/PKCE handshake primitives**, the **Oura OAuth client** (authorize URL, PKCE code exchange, refresh), the **OAuth linking service**, the **`connection.initial_sync` handler** with the Oura V2 fetch client and atomic ingestion commit, the **Oura sleep normalizer** writing versioned canonical facts, the **OIDC login flow** with hash-only opaque sessions, the authenticated **`/v1/sleep` deterministic summary** (adherence + 14-day debt, a summary not a score), the authenticated **`/v1/recovery` surface** running the full `general_recovery_v0.1.0` scoring path (a real score once overnight HRV/RHR ingest, else honestly `insufficient`), the **overnight-vitals ingestion** (HRV/RHR/temperature/respiratory from the Oura sleep payload), the composite **`/v1/today`** view stitching recovery and sleep, **versioned score persistence** (`daily_health_scores`/`score_factors`), and the **`score.recompute` handler chained after `raw.normalize`** so scores recompute automatically as data lands. The authenticated **`POST /v1/checkin`** write path feeding the subjective component, and recovery/today surfaces that **serve the persisted score** (disclosing its version and freshness), falling back to compute-on-read only for a day never scored. All nine recovery components have their formulas and can activate from real data (prior-load from `workout_sessions`). The deterministic **anomaly detectors + persistence** (open/2-day-clear intervals, detected automatically during `score.recompute`), **training label**, and **recommendation rules** (Stage 4/5) are implemented; the training label + primary/supporting recommendations ship on `/v1/today`, a persisted high-severity anomaly floors the label at `light`, the day's descriptive ACWR (from workout load) drives the over-load downshift, and a high check-in symptom burden floors the label at `light`. The **typed tool registry** (AI-independent) is exposed over `/v1/tools`. Each recomputed score records a **derivation run** with an opaque, tenant-scoped **provenance token** — `/v1/today` returns it as `provenance_url` and `GET /v1/provenance/{token}` resolves it to disclosed lineage (versions, status, input roles) that leaks no ids. **Canonical zone-load** and the **Polar workout normalizer** activate the prior-load/ACWR path from `workout_sessions`, flowing through the ingestion loop via schema-version dispatch. The **Polar fetch client** (AccessLink exercises) ships alongside the workout normalizer, and a **provider-parameterized backfill config** (`sync_config_for_provider`) lets the same initial-sync handler backfill a linked Polar connection into `workout_sessions` (proven end to end). The **Polar OAuth client** (`PolarOAuthClient`: authorize URL + Basic-auth code exchange, no PKCE, no refresh token, capturing Polar's `x_user_id` as the connection's `external_user_id`) ships too. The **Google Health v4 connector** ships its **v4** fetch client (`dataPoints` GET with an AIP-160 `filter` over `sleep.interval.start_time` and `nextPageToken` pagination) and a pure sleep-segment normalizer that aggregates stage segments into one canonical session per wake-date; the same initial-sync handler backfills a linked Google Health connection into `sleep_sessions` (proven end to end). With two sleep providers now possible, a deterministic **sleep source precedence** (`source_policy_v0.1.0`: Oura authoritative, Google Health fallback) selects one provider per day in the sleep feature queries — no averaging, no cross-provider fallback — so a second connector cannot double-count a night; that decision is also **written on every normalize** as a versioned `source_selections` row (daily-metric slice), with the losing providers recorded as candidates for the "Why" — never a blend, never a silent fallback. The **Google Health OAuth client** (`GoogleHealthOAuthClient`: PKCE authorize URL with `access_type=offline`/`prompt=consent`, form-body client_secret code exchange, refresh) ships too. The **OAuth linking service now handles all three providers uniformly** — a `uses_pkce` flag on the client port makes it thread a PKCE verifier only for Oura/Google Health and seal an empty placeholder for Polar, so a full authorize→callback link works for a non-PKCE provider too (with Polar's `x_user_id` landing as the connection's external user id). The **authenticated connector link routes** ship: `GET /v1/connections/{provider}/authorize` + `/callback` take the tenant from the session, resolve per-provider OAuth credentials from config, and drive the linking service — proven end to end over HTTP that a browser can link a Polar connection to `active`. An unconfigured or unknown provider is an indistinguishable 404. **Incremental sync** (`connection.incremental_sync`) resumes a connection from its stored cursor (minus an overlap) rather than re-pulling a full lookback, sharing the initial-sync page loop and degrading to a lookback on the first run. **Webhooks** ship for all three providers: `POST /webhooks/{provider}/{connection_id}` verifies an HMAC-SHA256 body signature in constant time (Oura/Polar) or a Google-signed push OIDC token against Google's rotating JWKS (Google Health), records the delivery once in a deduplicated `webhook_inbox`, and enqueues an incremental-sync refetch — the delivered body is never trusted as data, only as a trigger. A scheduled **reconciliation sweep** (`connection.reconcile_sweep`) catches gaps a missed webhook or sync would leave: it enqueues an incremental sync (idempotency-keyed per connection) for any active connection whose last successful sync is stale. **Daily activity ingestion** (`google_activity_v0.1.0`: steps + active minutes → `daily_activity`, via the `google_activity.` schema dispatch) now feeds the **low-activity anomaly** — a far-below-baseline steps day opens a `low_activity` interval, so all six v0.1.0 detectors are sourced. The **authenticated `/v1` surface** has since widened: `GET /v1/connections` (per-connection sync status), `POST /v1/connections/{id}/sync` (manual sync, `Idempotency-Key`), `GET /v1/anomalies` (active and recently-cleared non-diagnostic flags), `GET /v1/workouts` + `/{id}` (cursor-paginated sessions with zone minutes), and `POST /v1/privacy/delete` + status (irreversible, synchronous erasure). The **internal debug router is gone**, so no route accepts a client-supplied `tenant_id` anywhere. Job side effects are fenced by a **unit of work** that re-checks the job lease inside the write transaction, so a worker whose lease expired mid-compute cannot supersede the row the rightful owner wrote. **Confirmation enforcement** ships for mutating tools: `connections.sync` executes only against a one-time, expiring confirmation bound to the exact call. Not implemented: the `activity`/`strain` **scores** (blocked — no accepted formula/fixtures), the session/workout `source_grains`/`source_grain_versions`/`source_grain_members` grain machinery (the daily-metric `source_selections` slice ships; the versioned-membership tables for episode matching do not, and are deferred until a second workout provider makes cross-provider matching a real need), and workout overlap exclusion (same prerequisite).

**Implemented storage scope:** local **libSQL / Turso-compatible** `sqlite+libsql` only (in-memory or file). **Turso Cloud / remote** is intentionally deferred by product decision — not wired in this foundation and **not** blocked on credentials. Long-term production Turso architecture remains documented under `docs/` as proposed future context (ADR 0003, architecture pages).

## Requirements

| Item | Policy |
|------|--------|
| Python | **3.13.14** only (`requires-python = ">=3.13.14,<3.14"`) |
| Dependencies | **Exact pins** of latest **stable** releases as of 2026-07-13 (`cryptography==49.0.0` 2026-07-18; `httpx2` promoted to runtime, `pyjwt==2.13.0` added 2026-07-19) — **no prereleases** |
| Database dialect | Official `sqlite+libsql` via `sqlalchemy-libsql==0.2.0` (local forms only) |
| Model SDKs | **Forbidden** in core install (openai, anthropic, gemini, xai, openrouter, local-model stacks, …) |

### Python compatibility gate (honest)

On **macOS ARM**, **Python 3.14.5 + sqlalchemy-libsql 0.2.0** was observed to **segfault**. The same driver works on **Python 3.13**. This foundation therefore pins **3.13.14** and rejects 3.14 until the driver/runtime stack is re-validated.

## Setup

```bash
cd backend
uv python install 3.13.14
uv sync --all-groups
```

## Tests and quality gates

```bash
uv run ruff check
uv run ruff format --check
uv run mypy src tests
uv run lint-imports
uv run pytest
uv lock --check
uv tree --outdated
uv run pip-audit
```

These same gates run in CI (`.github/workflows/backend.yml`) across four jobs: **quality** (lint, format, types, contracts, tests), **migrations** (upgrade → downgrade to base → upgrade on an ephemeral DB), **boot-boundary** (installs with `--no-dev`, asserts no model SDK is importable, then boots API and worker with no `MODEL_*` config), and an advisory **audit**. No model or provider credentials are set anywhere in the workflow — that absence is itself part of the "models disabled" exit criterion.

## Run API

```bash
# optional: export AKUNAKI_DATABASE_URL=sqlite+libsql:////abs/path/to/file.db
uv run python -m akunaki.api
# GET http://127.0.0.1:8000/healthz   # cheap liveness: process up + DB reachable
# GET http://127.0.0.1:8000/readyz    # deep readiness for probes/dashboards
```

`/readyz` is the deployment readiness probe: it reports the **migration head** (is the DB at the code's revision?), **queue depth** (ready/leased/dead-letter job counts), and whether a worker holds the **`core-reaper` leader lease**. It returns **503** unless the DB is reachable *and* at the migration head — so a deployment whose migrations haven't run reads not-ready. Queue depth and leader presence are reported for dashboards but do not gate readiness. It is read-only, so probing it never perturbs the queue or leases.

Every response carries an **`X-Request-ID`** correlation id — a trusted inbound one (bounded/validated so it can't poison logs) or a fresh UUID — bound to a `ContextVar` for the request. A logging filter puts that id on every log record, and the entrypoint's JSON log format includes `request_id`, so a request's logs across all layers share one id.

## Run worker

```bash
uv run python -m akunaki.worker
```

Boots core config/DB, probes readiness, then runs the durable claim loop until `SIGINT`/`SIGTERM` requests a cooperative shutdown (the in-flight job settles first).

Each iteration claims one due job by fenced CAS, runs its registered handler while a background thread extends the lease, and settles the outcome durably:

| Outcome | Effect |
|---------|--------|
| Handler returns | `complete_job` under the original fence; a lease lost mid-run suppresses completion rather than reporting false success |
| `TransientJobError` (or unknown exception) | Retry scheduled with capped exponential backoff + jitter, until `max_attempts` |
| `PermanentJobError`, `ValueError`/`TypeError`/`KeyError` | Dead-lettered immediately without burning the attempt budget |
| Unregistered `job_type` | Dead-lettered as `UnregisteredJobType` (deployment error, not transient) |

Only the holder of the `core-reaper` **leader lease** requeues expired leases and dead-letters exhausted ones, so a passive standby never reaps behind an active worker. The same leader also fires **periodic jobs** (`JobWorker(schedules=[...])`): the pure `due_schedules` picks which are due on each reaper tick, and each fire is idempotency-keyed so a lost lease or a crash mid-fire never duplicates. The entrypoint wires the **reconcile-sweep schedule** (every 30 min, owned by the reserved `system` tenant) and the **full product handler registry** (`build_registry`), so the sweep's fan-out incremental syncs actually run. A single **`ProviderDispatchSyncHandler`** routes each sync to the per-provider handler for its connection's provider, so one worker syncs Oura, Polar, and Google Health. (The worker needs a KEK configured — a real sync opens sealed connection tokens.)

Execution policy lives in `akunaki.application.worker_runtime` (port-typed, no SQLAlchemy); durability lives in `JobRepository`. Handlers register in `akunaki.application.handlers`; `system.noop` and `connection.initial_sync` ship today. Handlers **must be idempotent** — a lease can expire mid-run and the job be retried elsewhere.

## Enqueue work

`JobRepository.enqueue_job` is how work enters the durable lifecycle:

```python
result = repository.enqueue_job(
    job_id="job-1",
    tenant_id="tenant-1",
    job_type="connection.initial_sync",
    payload_json='{"connection_id":"c1"}',
    now=datetime.now(UTC),
    idempotency_key="tenant-1:c1:initial",   # optional
)
result.created  # False when an existing job for this key was returned
```

Deduplication is on `(tenant_id, idempotency_key)` via an atomic `INSERT ... ON CONFLICT DO NOTHING`, so a retried API call, a redelivered webhook, or a re-run scheduler cannot fan out duplicates — and concurrent enqueues of one key neither double-insert nor raise. A `None` key always inserts (SQL `NULL` never conflicts). `run_after` defaults to `now`; pass a future time to schedule. A repeated `job_id` **without** a key raises, since that is a caller bug rather than a dedupe.

## Migrations

```bash
export AKUNAKI_DATABASE_URL=sqlite+libsql:////abs/path/to/file.db
uv run alembic upgrade head
uv run alembic downgrade 20260722_0018   # drop workout sessions
uv run alembic downgrade 20260721_0017   # also drop anomalies
uv run alembic downgrade 20260720_0016   # also drop subjective check-ins
uv run alembic downgrade 20260720_0015   # also drop respiratory column
uv run alembic downgrade 20260720_0014   # also drop temperature column
uv run alembic downgrade 20260720_0013   # also drop daily health scores
uv run alembic downgrade 20260719_0012   # also drop overnight vitals
uv run alembic downgrade 20260719_0011   # also drop oidc login states
uv run alembic downgrade 20260719_0010   # also drop users and sessions
uv run alembic downgrade 20260719_0009   # also revert tenant-scoped fact indexes
uv run alembic downgrade 20260719_0008   # also drop deletion pipeline
uv run alembic downgrade 20260719_0007   # also drop per-record slice body
uv run alembic downgrade 20260719_0006   # also drop sleep fact schema
uv run alembic downgrade 20260718_0005   # also drop sync transport schema
uv run alembic downgrade 20260718_0004   # also drop oauth state schema
uv run alembic downgrade 20260713_0003   # also drop connection lifecycle schema
uv run alembic downgrade 20260713_0002   # also drop attempt/dead-letter lifecycle schema
uv run alembic downgrade 20260713_0001   # also drop lease tables
uv run alembic downgrade base
uv run alembic upgrade head
uv run alembic current
```

| Revision | Tables |
|----------|--------|
| `20260713_0001` | `tenants`, `jobs` |
| `20260713_0002` | `job_leases`, `leader_leases` |
| `20260713_0003` | job type/error fields, `job_attempts`, `job_dead_letters` |
| `20260718_0004` | `connections`, `connection_secrets`, `connection_health` |
| `20260718_0005` | `oauth_states` (hashed state + sealed PKCE verifier) |
| `20260719_0006` | `sync_runs`, `raw_payload`, `sync_cursors`, `raw_objects`, `raw_revisions` |
| `20260719_0007` | `fact_records`, `sleep_sessions` (sleep slice only) |
| `20260719_0008` | `raw_revisions.slice_json` (per-record body) |
| `20260719_0009` | `deletion_requests`, `deletion_completion_proofs` |
| `20260719_0010` | tenant-scoped fact identity indexes |
| `20260719_0011` | `users`, `sessions` (hash-only token storage) |
| `20260719_0012` | `login_states` (hashed state + nonce, sealed PKCE verifier) |
| `20260720_0013` | `overnight_vitals` (HRV, resting HR detail) |
| `20260720_0014` | `daily_health_scores`, `score_factors` (versioned scores) |
| `20260720_0015` | `overnight_vitals.temperature_deviation_c` (widened invariant) |
| `20260720_0016` | `overnight_vitals.respiratory_rate_bpm` (widened invariant) |
| `20260721_0017` | `subjective_check_ins` (versioned; the first user write) |
| `20260722_0018` | `anomalies` (tracked open/closed intervals) |
| `20260722_0019` | `workout_sessions` (canonical zone-load detail) |
| `20260722_0020` | `derivation_runs` + `derivation_inputs` (lineage + opaque provenance); `daily_health_scores.derivation_run_id` |
| `20260723_0021` | `webhook_inbox` (durable deduplicated deliveries; one-way FK to `raw_payload`) |
| `20260723_0022` | `daily_activity` (steps + active minutes detail; at-least-one-signal) |
| `20260724_0023` | `source_selections` + `source_selection_candidates` (versioned daily source-precedence decision) |
| `20260724_0024` | seed the reserved `system` tenant (owns system-wide periodic jobs) |

### Sync transport layer (`0006`)

Two layers with deliberately different dedupe rules:

| Layer | Tables | Rule |
|-------|--------|------|
| Transport | `raw_payload` | **Every** vendor response is retained. `content_hash` is *indexed, not unique*, so a retried fetch writes a new row. |
| Logical | `raw_objects`, `raw_revisions` | Append-only. A new revision is skipped when that object already has the same `content_hash`. |

This split is what makes crash replay safe: a crash before commit leaves cursors unchanged so the same window can be refetched, and the logical hash check stops the retry from creating duplicate revisions while the transport row is still kept for audit.

Other enforced invariants: `raw_payload.sync_run_id` is **nullable** (a webhook body can land before a run exists); `payload_json` and `payload_blob` are mutually exclusive; `revision_n` is unique per object; and `tombstone_reason` accepts only `vendor_deleted` or `privacy_delete` — **`superseded` is rejected**, because superseding is expressed by a later revision, not by marking the old one deleted. There is no `normalizer_version` on raw rows; that belongs on facts.

`webhook_inbox` is **not** created yet — it arrives with webhook handling, keeping the inbox→payload FK one-way.

### Initial sync (`connection.initial_sync`)

The first product job handler. Enqueue it after a successful link:

```python
repository.enqueue_job(
    job_id=new_id(), tenant_id=tenant_id,
    job_type=INITIAL_SYNC_JOB_TYPE,
    payload_json=json.dumps({"connection_id": connection_id}),
    now=now, idempotency_key=f"{tenant_id}:{connection_id}:initial",
)
```

The handler opens the connection's sealed tokens, fetches windowed pages from Oura V2, and commits each page atomically. Fetch outcomes map onto the worker's retry vocabulary:

| Outcome | Handler behavior |
|---------|------------------|
| 401 / 403 | Flip connection to `needs_reauth`, then **dead-letter** — retrying a dead grant only burns the attempt budget |
| 429 | Connection → `error`, raise `TransientJobError`; `Retry-After` is surfaced in the message |
| 5xx / transport / malformed body | Connection → `error`, retry with backoff |
| Success | Connection → `active`, cursor advanced |

Backfill lookback defaults to 90 days plus a 36h overlap, but is configurable via `SyncConfig` because the 30-vs-90 choice is still an open product decision. `max_pages` bounds runaway pagination.

A successful page commit also enqueues a `raw.normalize` job **in the same transaction** as the revision, so a revision can never exist without its normalization job — and a crash before commit leaves neither.

**Per-record identity.** A fetched page is split (`akunaki.domain.record_split`) into one logical record per entry: one transport row is retained whole, but each record gets its own `raw_object`, its own append-only `raw_revision`, and its own `slice_json` body. A vendor correcting one night therefore revisions only that night, and each normalize job parses only its own record.

Records are keyed `stream:<vendor_id>` when the vendor supplies an id. **Remaining gap:** streams without one fall back to `stream:hash:<body_hash>` — still per-record, but a cosmetic vendor change re-identifies the record. Only `sleep`, `daily_*`, and `workout` have mapped id fields today.

### Sleep facts and normalization (`0007`)

`fact_records` is the header row every normalized measurement gets; typed detail lives in a one-to-one table keyed by `fact_record_id` (**not** EAV, not a table-name string pointer). Only `sleep_sessions` ships today — the other detail tables arrive with the normalizers that populate them.

**Facts are versioned, never updated in place.** Writing content identical to the current version is a no-op; changed content supersedes it and appends `version_n + 1`, retaining the prior row *and its detail* for provenance. A partial unique index (`fact_key WHERE is_current = 1`) is the schema-level backstop: a logical fact can have at most one current version.

`fact_key` is an addition beyond the documented column list — the data model describes versioning but names no column identifying a logical fact across its versions. It is derived (`sleep_session:<vendor_record_id>`), so it introduces no new source of truth.

The normalizer (`akunaki.domain.sleep_normalizer`) is **pure**: no I/O and no clock, so re-running it over the same raw revision produces byte-identical facts. Canonical rules it applies:

| Rule | Behavior |
|------|----------|
| Wake-date assignment | A bout is assigned to the local date of **wake**, not onset — a 23:10→07:20 night counts for the morning it ended |
| Canonical units | Vendor seconds become minutes; steps stay integers, energy kcal, distance metres |
| Quality grading | Missing stage detail lowers `quality`/`confidence` rather than presenting a partial night as complete |
| Bad records | One unusable record is skipped, never failing the whole page |

### Normalization (`raw.normalize`)

Enqueued automatically by a successful sync commit, keyed by `raw_revision_id`. The handler reads the immutable revision and **dispatches by schema version**: an Oura sleep page (`oura.*`) normalizes into both **sleep** and **overnight-vitals** facts; a Polar exercise page (`polar.*`) normalizes into **workout** facts with internally computed zone-load. It writes versioned facts and enqueues a `score.recompute` for each affected local health day.

| Outcome | Behavior |
|---------|----------|
| Missing revision, malformed payload, unparseable body | **Dead-letter** — none of these fix themselves on retry |
| Tombstone revision | Skipped (vendor deletions use the deletion path, not a fabricated fact) |
| Success | Facts written; identical content writes no new version; recompute enqueued (keyed `recompute:<revision>:<day>`, so a retry dedupes but a correction re-scores) |

### Score recompute (`score.recompute`)

Chained after normalize. The handler assembles the recovery surface for the day (`general_recovery_v0.1.0`) and persists it as a versioned score row via `ScoreRepository`. Persistence is idempotent by `dependency_hash`, so a redundant recompute writes no new version. An `insufficient` day is a real, stored outcome — not an absence. The full chain **sync → normalize → recompute → persisted score** is proven end to end through the real worker runtime.

The handler also **detects and tracks anomalies** for the day: `RecoveryInputService.feature_signals` computes each feature's robust z-score (the same z its recovery component used) and the `AnomalyTracker` advances the interval state machine — opening a new interval when a detector fires, counting clear days, and closing after two consecutive clears. A far-below-baseline HRV opening a `low_hrv` interval during recompute is proven end to end.

### Privacy deletion (`0009`)

The phase-one **stub**: cancel the tenant's work, scrub its rows, write a minimal proof.

Ordering is a **safety property**, not bookkeeping — jobs are cancelled first, in their own committed transaction, so no in-flight sync can re-insert rows the scrub is about to delete. The state machine rejects skipping a stage:

```
requested -> jobs_cancelled -> rows_scrubbed -> backups_scheduled -> completed
                    (any stage may transition to failed)
```

`deletion_requests` deliberately has **no FK to `tenants`** — the request must outlive the tenant it scrubs, or completing a deletion would erase its own audit trail. The completion proof stores **counts only**: no tenant id, no display name, no health values.

**Not built:** the restoration-suppression ledger (needs a dedicated deletion key with access separation — an empty table would imply a guarantee the system cannot make), and actual backup expiry (no backup provider is wired; the pipeline records the stage only).

### No unauthenticated tenant surface

The internal debug router is **gone**, along with its `AKUNAKI_DEBUG_ROUTES_ENABLED` flag. Its two routes were superseded: `latest-sleep` by the authenticated `/v1/sleep`, and `sync-status` by `GET /v1/connections` (below).

That removal cleared the last route that took a `tenant_id` as a **query parameter**. **No route anywhere accepts a client-supplied tenant** — every product surface derives it from the validated session. The only unauthenticated routers left are ones that must be:

| Router | Why it is unauthenticated |
|--------|---------------------------|
| `/auth/*` | Login itself; there is no session yet |
| `/healthz`, `/readyz` | Probes; no tenant data, no writes |
| `/metrics` | Scraper cannot hold a cookie. PHI-free by construction (bounded label tokens, never a tenant id or health value) and off unless `AKUNAKI_METRICS_ENABLED` |
| `/webhooks/*` | The vendor calls it; trust comes from the signature or Google-signed push token |

### Audit events (tamper-evident)

Destructive actions append an `audit_events` row: **what happened, to what, by whom — never a health value.** This is the control for repudiation ("I didn't delete that").

Two properties are enforced:

- **No health values.** `metadata_json` is a bounded key/value map, and a key shaped like a measurement (`hrv_ms`, `recovery_score`, `sleep_min`, `steps`, …) is **rejected**, not filtered — silently dropping a key would make an incomplete record look complete.
- **Tamper-evident.** Each event hashes its own content *plus* the previous event's hash, so editing, removing, or reordering a row breaks every link after it. `AuditRepository.verify()` returns the `seq` of the first bad event. A `GENESIS_HASH` sentinel means a truncated chain cannot pass as a fresh one.

`audit_events` deliberately has **no FK to `tenants`**: the record must outlive the tenant it describes, or a privacy deletion would destroy its own proof.

**Verification runs on a schedule.** A leader-gated `audit.verify_chain` job (hourly, system tenant) walks the chain and publishes two gauges:

| Metric | Meaning |
|--------|---------|
| `akunaki_audit_chain_intact` | 1 when the last pass found no tampering, 0 when it did |
| `akunaki_audit_chain_verified_timestamp_seconds` | When that pass ran — a **stale** value is itself an alert that the verifier stopped |

It runs on the worker rather than behind an endpoint: verification is O(chain), so a route would hand any caller an unbounded scan. Those gauges live in the **worker's** registry — each process serves its own, and the worker serves no scrape endpoint — so the verdict is also **persisted** to `system_checks` and surfaced on `/readyz`:

| `/readyz` field | Meaning |
|-----------------|---------|
| `audit.events` / `audit.last_event_at` | O(1) tail read; a **stale** timestamp while audited actions continue means the trail stopped being written |
| `audit.chain_intact` | Stored verdict of the last verification. **Null** when it has never run — unknown and tampered must not read alike |
| `audit.chain_checked_at` | When that verification ran |

All four are reported, never gating: a tampered trail does not make the service unable to serve traffic. Detected tampering does **not** fail the job — tampering is not transient, so a retry would dead-letter and turn a standing alert into a one-off error. The gauge stays at 0 until a pass succeeds.

`verify()` walks in bounded batches; the table only grows, so materializing it would fail first on the deployment with the most history to protect.

**Honest limit:** a hash chain detects row-level tampering by someone editing the database. It does **not** defend against an attacker who can rewrite the whole chain — that needs signed batches or an external anchor, which is not built.

Wired today:

| Action | When |
|--------|------|
| `delete` | Every `privacy.delete`, on success **and** failure — a half-run erasure is the case most worth recording |
| `tool.invoke` | Every **mutating** tool call: succeeded, failed, or **refused** |
| `connection.create` | Every OAuth callback: linked, failed, or cross-tenant refused |

**Reads are deliberately not audited.** The threat this answers is confused deputy — *actions taken*, not data read — and every append serializes on a tail read, so auditing the seven read tools would put a global write lock on the hottest path and add thousands of rows a day that answer no security question. `Tool.is_audited` encodes the rule.

**Refusals are recorded.** A rejected confirmation is what a confused-deputy attempt looks like from outside; auditing only successes would leave exactly the attempts worth investigating with no trace. An agent-originated call also records `origin=agent_run`.

Neither path copies tool arguments or token material into the trail — the confirmation binding's args hash is the non-health handle to a mutation's exact inputs. `export` is in the vocabulary but unemitted (no export service).

### Retention sweep (expired credential material)

Sessions, OAuth/PKCE states, login states, and tool confirmations all carry an `expires_at` and all stop being usable the moment it passes. A leader-gated `retention.sweep_expired` job (hourly, system tenant) deletes them, so their stored secrets — hashed session tokens and CSRF secrets, sealed PKCE verifiers, confirmation token hashes — do not outlive the window they were issued for. Each of these tables already carried an `expires_at` index for exactly this sweep.

Deletion is on **expiry alone**, never on status, so the job is safe to run unattended: an expired row cannot authenticate, cannot complete an OAuth exchange, and cannot authorize a mutation, so nothing still in use can be swept away. A **consumed** confirmation is kept until it expires — its job is to make a replay fail, and past expiry it fails on expiry alone.

One store failing does not abort the sweep; the rest still run and the job raises at the end so the worker's retry policy sees it.

### Security headers and CORS

Every response — including errors — carries a strict set of headers, applied by middleware so no route can forget them: a `default-src 'none'` CSP (this is a JSON API that renders no document), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and same-origin COOP/CORP.

Cross-origin browser access is **opt-in** via `AKUNAKI_CORS_ALLOWED_ORIGINS`: credentialed CORS is granted only to the exact origins on the allow-list (never `*` with credentials), for `GET`/`POST` with the `content-type` and `x-akunaki-csrf` headers. An empty allow-list (the default) means no cross-origin browser access — a same-origin or server-to-server deployment.

### Sessions (`0011`)

Backend-issued opaque sessions. The raw cookie token is generated at issue time, returned **once**, and never written: only `token_hash` and `csrf_secret_hash` are stored, so a database dump yields no usable session and lookup is an index probe on the hash.

```python
issued = sessions.issue(session_id=new_id(), user_id=user_id, now=now)
# issued.token -> cookie;  issued.csrf_secret -> client
result = sessions.validate(token=cookie_token, now=now)   # typed rejection, not an exception
sessions.rotate(old_token=..., new_session_id=..., now=now)  # revokes the predecessor
```

`validate` returns a typed `SessionRejection` (`not_found` / `expired` / `revoked`) so callers surface one generic `401` without revealing which check failed. Rotation issues the successor **before** revoking the old session, so a crash between the two leaves the user logged in rather than stranded.

Cookie and CSRF enforcement live in `akunaki.api.security`:

| Rule | Behavior |
|------|----------|
| Cookie | `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/` — Lax rather than Strict so an IdP redirect back to us still carries the cookie |
| CSRF | Required on `POST`/`PUT`/`PATCH`/`DELETE` via `X-Akunaki-CSRF`, checked against the session's own secret; a **403**, since the caller is authenticated but the request is not attributable |
| Rejections | One generic `401` for unknown / expired / revoked, so valid tokens cannot be enumerated |
| Tenant | Taken from the validated session, never from a request parameter |
| Logout | Server-side revoke **and** cookie clear; clearing alone would leave a captured token usable |
| Logout everywhere | `POST /v1/session/logout-everywhere` revokes **every** live session for the user (stolen-device / password-change), leaving other users untouched |

### OIDC login primitives (`0012`)

The IdP is **self-hosted Authelia** (roadmap decision 1). `login_states` is deliberately separate from `oauth_states`:

| Reason | Detail |
|--------|--------|
| No tenant yet | `oauth_states.tenant_id` is a required FK, but login is what *establishes* the tenant |
| Different provider vocabulary | `oauth_states.provider` is constrained to data providers; loosening it would weaken a real guard on the connector path |
| OIDC needs a nonce | `state` protects the redirect against CSRF; `nonce` binds the returned `id_token` to this specific request |

`state` and `nonce` are stored **hashed**; the PKCE verifier is **envelope-encrypted**. Consumption is single-use via an atomic conditional `UPDATE`, so a replayed callback loses.

`akunaki.domain.oidc.validate_id_token_claims` checks `iss`, `aud` (string or array), `nonce`, `exp`/`nbf`/`iat` (60s skew), and `sub`. It is pure with an injected clock, and **assumes the signature was already verified** against the issuer's JWKS — it never treats an unverified token as valid.

`akunaki.adapters.oidc.OIDCClient` handles the network and signature parts: discovery (cached, issuer confirmed against config), the PKCE authorize URL, token exchange, and `id_token` **signature** verification via PyJWT against the issuer's JWKS. It accepts **asymmetric algorithms only** — an HS256 token forged with a known public key is refused, closing the alg-confusion class. Signature verification lives here; the pure `domain.oidc` validator owns every *claim* policy against an injected clock, so PyJWT's real-time `exp`/`nbf` checks are turned off to keep one authority over time.

The `/auth/login` and `/auth/callback` routes wire it together, mounted **only when OIDC is configured** (`AKUNAKI_OIDC_ISSUER` set):

```bash
export AKUNAKI_OIDC_ISSUER=https://auth.example.com
export AKUNAKI_OIDC_CLIENT_ID=akunaki-web
export AKUNAKI_OIDC_CLIENT_SECRET=...
export AKUNAKI_OIDC_REDIRECT_URI=https://app.example.com/auth/callback
```

`GET /auth/login` returns the authorize URL; `GET /auth/callback` verifies the token, provisions the user on first login (one user per tenant, keyed by `(oidc_issuer, oidc_subject)`), sets the session cookie, and returns the CSRF secret. The orchestration (`akunaki.application.login`) seals state before the redirect, consumes it single-use, and verifies the token **before** any session is issued.

Login now works end to end — `/v1` is reachable behind a cookie session.

### `GET /v1/sleep` — deterministic sleep summary

The first authenticated product surface. It answers with a **deterministic summary, not a score**: measured sleep duration against a target, bounded adherence, and the rolling 14-day sleep debt. The design forbids implying a sleep score exists, so the response carries no score field of any kind.

```bash
curl --cookie akunaki_session=<token> 'localhost:8000/v1/sleep?day=2026-07-19'
```

| Property | Rule |
|----------|------|
| Tenant | From the validated session, never the query string — a caller cannot read another tenant's sleep |
| Duration | Total sleep minutes for the day, summed across all current sessions (naps and splits included, per the data model) |
| Adherence | `sleep_summary_v0.1.0`: `clamp(100 * (1 - shortfall/target), 0, 100)`; oversleep earns no bonus |
| Debt | 14-day window (the day plus the previous 13); per known day `credit = min(surplus, 60)`, `debt = clamp(debt + shortfall - credit, 0, 14*target)` |
| Unknown days | **Skipped, never imputed as zero**; the window is marked `partial` and the debt disclosed as a lower bound |
| Recommendations | Gated on `>= 12` known days in the window |

The arithmetic lives in the pure `akunaki.domain.sleep_summary` (golden-tested against hand-computed values); `akunaki.application.sleep_surface` fetches the window durations and the route only shapes the response. Verified end to end over real HTTP, including tenant isolation and the no-score-leak guarantee.

### `GET /v1/recovery` — the one shipping score

Recovery is the **only** 0-100 score in v0.1.0 (`general_recovery_v0.1.0`). The surface runs the full assembled scoring path — windowed baselines → robust z-scores → directed component mapping → weighted mean over present weights — and discloses everything: `status`, `score`, `confidence`, `available_weight`, the present `factors`, and any `data_gaps`.

```bash
curl --cookie akunaki_session=<token> 'localhost:8000/v1/recovery?day=2026-07-20'
```

The sufficiency gate requires an authoritative sleep duration **and** HRV or overnight RHR **and** ≥ 0.60 available weight. Overnight HRV/RHR now ingest from the Oura sleep payload (`overnight_vitals`), so a tenant with a mature vitals baseline gets a real 0-100 score with HRV/RHR among its factors. A tenant without HRV/RHR is honestly `insufficient` with a null score, disclosing `missing_hrv_or_resting_hr` in `data_gaps` — never a fabricated midpoint.

| Layer | Responsibility |
|-------|----------------|
| `domain.baseline` | 42-day rolling window, median center, MAD→robust_scale (IQR/floor fallback), maturity gate, clamped z |
| `domain.recovery` | component weights, z→c curve, gate, weighted mean, confidence, and `recovery_data_gaps` |
| `domain.recovery_components` | z→directed `c`; insufficient baseline → omit (never a midpoint) |
| `application.recovery_inputs` | fetch windowed sleep features → present components |
| `application.recovery_surface` | evaluate + package with factors and gaps |

An end-to-end test guards the cardinal rule at the HTTP boundary: an insufficient recovery must expose `score: null`.

The surface **serves the persisted score** (`ServedRecoveryService`): it reads the current `daily_health_scores` row and its factors, re-deriving the disclosed `data_gaps` from the present factor codes with the same pure rule the live evaluation uses, and discloses the stored `version_n` and `freshness_at`. A day that has never been scored — no recompute has fired for it — falls back to computing on read (with null version/freshness), so a response is never empty just because the job has not run. The compute path remains for the recompute job itself.

### `GET /v1/today` — the composite day view

The primary product read surface. It stitches the two shipping blocks — the recovery score and the sleep summary — and discloses everything else rather than inventing it.

```bash
curl --cookie akunaki_session=<token> 'localhost:8000/v1/today?day=2026-07-20'
```

| Rule | Behavior |
|------|----------|
| Top-level `status` | Mirrors the recovery status (recovery is the day's headline score) |
| Recovery block | The only 0-100 score; currently `insufficient` with a null score |
| Sleep block | The deterministic summary; **absent** on a no-sleep day (no phantom zero-duration measurement) with `missing_authoritative_sleep` disclosed |
| Training recommendation | The deterministic `training_label_v0.1.0` label (`hard`/`moderate`/`light`/`rest`/`insufficient`) — not a numeric readiness score |
| Recommendations | At most one `primary_recommendation` plus `supporting_recommendations`, selected by the exact rule predicates with priority/conflict-group resolution |
| Strain / activity | **Do not ship** in v0.1.0 — absent from the body and named as `strain_not_available` / `activity_not_available` gaps |
| Gaps | Deduplicated across the composite and the recovery gate |

The composite owns no formula: `akunaki.application.today_surface` delegates to the recovery and sleep surface services and combines their disclosures, then applies the pure training-label and recommendation rules. A persisted **active high-severity anomaly floors the training label at `light`** (read via `AnomalyRepository`). The day's **descriptive ACWR** now feeds the load rules too — read from `RecoveryInputService.acwr_for_day`, the same acute/chronic load windows the prior-load recovery component uses, so the ratio and the component can never disagree; an over-load ratio (> 1.3) downshifts a `hard` day, and an undefined ratio (partial coverage or full rest) leaves the label untouched. The **symptom burden** from the day's completed check-in feeds the label the same way — read from `RecoveryInputService.symptom_burden_for_day`, the same check-in the subjective recovery component uses — so a burden ≥ 0.75 floors a `hard` day at `light`, while an absent check-in leaves it untouched (the `severe_symptom_flag` REST trigger stays dormant: the v0.1.0 check-in captures no distinct severe-symptom marker). Verified end to end, including that unshipped blocks never appear as fabricated data.

### `POST /v1/checkin` — the first write path

A user's completed daily check-in, feeding the subjective recovery component. This is the first authenticated **write**, so it requires both the session cookie and the CSRF header (`X-Akunaki-CSRF`, echoed from login) — `require_session` enforces CSRF on state-changing methods automatically.

```bash
curl -X POST --cookie akunaki_session=<token> -H 'X-Akunaki-CSRF: <secret>' \
  -H 'content-type: application/json' \
  -d '{"local_health_day":"2026-07-22","energy_n":0.6,"stress_n":0.4,"symptom_burden_n":0.2}' \
  localhost:8000/v1/checkin
```

The three inputs are normalized to [0, 1] (energy higher is better, stress and symptom burden higher are worse). The write is **versioned** — a re-submission for the same day supersedes the prior one. All three fields are required for the subjective component; per the design, a missing check-in or blank field omits the component rather than assuming a benign 50. An end-to-end test confirms a recorded check-in surfaces as the `subjective` factor in `/v1/recovery`.

### `/v1/tools` — the typed tool registry

The **typed capability facade** (`ToolRegistry`) is the phase-two exit criterion "tools usable by REST without model packages" made concrete: the same registry an agent or MCP adapter would use is exposed to a plain HTTP client. It imports no model/AI SDK.

```bash
curl --cookie akunaki_session=<token> localhost:8000/v1/tools   # list tools + metadata
curl -X POST --cookie akunaki_session=<token> -H 'X-Akunaki-CSRF: <secret>' \
  -H 'content-type: application/json' -d '{"input":{"day":"2026-07-22"}}' \
  localhost:8000/v1/tools/health.get_sleep
```

Each tool is a `Tool` with Pydantic `input_model`/`output_model` and declared metadata — `scopes`, `sensitivity`, `side_effect`, `model_exposure`, `requires_confirmation`. Tools wrap the surface services and carry no formula. The **tenant comes from the tool context**, never the input, so a tool can no more cross tenants than a direct route; invoke is a state-changing POST, so CSRF is enforced. A tool whose subject does not exist raises `LookupError`, mapped to a generic **404** so a tool cannot become an id-probing oracle.

Registered today:

| Tool | Side effect | Scope |
|------|-------------|-------|
| `health.get_today` / `get_recovery` / `get_sleep` | none | `read:health` |
| `health.find_anomalies` | none | `read:health` |
| `health.get_recent_workouts` / `get_workout` | none | `read:health` |
| `connections.list` | none | `read:connections` |
| `connections.sync` | `enqueue_job` | `write:connections` |
| `privacy.delete` | `destroy_data` | `delete:privacy` |

`connections.list` is scoped `read:connections`, **not** `read:health`: connection metadata is not health data, and folding it in would over-grant a caller that only needs a day view.

#### Confirmation for mutating tools

Each tool declares a **`ConfirmationPolicy`**, and the invoke route asks the tool rather than applying one global rule — the canonical registry states three different answers:

| Policy | Tools | Meaning |
|--------|-------|---------|
| `never` | all reads | The session's own authorization is the whole check |
| `if_agent` | `connections.sync` | A direct session call is already an explicit, CSRF-enforced human act; a call carrying a `run_id` must redeem a confirmation |
| `always` | `privacy.delete` | Confirmed for **every** caller. A CSRF token proves the request came from our page, not that the human meant to erase everything |

`privacy.delete` is additionally `model_exposure=False`: a model may be told the capability exists, but must never be the thing that invokes irreversible erasure.

A confirmation authorizes **one specific call**, bound to `tenant_id` + `user_id` + `run_id` + `tool_name` + **canonical args hash** + **idempotency key**. It is one-time and expiring, stored as a SHA-256 hash only (`tool_confirmations`, migration `0025`). Consumption is a conditional CAS inside the same transaction as the binding check. The practical effect: a model that swaps an argument between the user's approval and execution **does not execute**, and a replay runs the side effect exactly once. Every rejection is the same generic 403, so a caller cannot probe for valid tool names or live tokens.

`exports.create` is **not** registered — it needs its own service wiring.

#### Obtaining a confirmation

The user approves **out-of-band via API, not the model**:

```bash
# 1. approve the exact call; the token is shown once (only its hash is stored)
curl -X POST --cookie akunaki_session=<token> -H 'X-Akunaki-CSRF: <secret>' \
  -H 'content-type: application/json' \
  -d '{"tool_name":"privacy.delete","input":{},"idempotency_key":"del-1"}' \
  localhost:8000/v1/confirmations
# 2. present it at invoke, with the same arguments and key
curl -X POST --cookie akunaki_session=<token> -H 'X-Akunaki-CSRF: <secret>' \
  -H 'content-type: application/json' \
  -d '{"input":{},"confirmation_token":"confirm_...","idempotency_key":"del-1"}' \
  localhost:8000/v1/tools/privacy.delete
```

Confirmations expire in 5 minutes and are single-use. Requesting one for a tool that needs none is a **409** — handing out tokens nothing checks would make the confirmation step a rubber stamp.

### `/v1/provenance/{token}` — opaque derivation lineage

Every recomputed recovery score records a **derivation run** (formula version, status, confidence, freshness) linked from `daily_health_scores.derivation_run_id`. The run mints a `provenance_token`: a **public but unguessable** handle — stored in the clear and unique-constrained (`uq_derivation_token`), so it is a URL slug, not a secret. `/v1/today` returns it as `provenance_url`.

```bash
curl --cookie akunaki_session=<token> localhost:8000/v1/provenance/opaque_tok_...
```

The lookup is authenticated and **tenant-scoped**. It discloses the artifact kind, versions, status, freshness, and the **roles** of the inputs — never a table, raw, or run id. An unknown token and a token owned by another tenant are the **same 404**, so a token cannot be probed for cross-tenant existence.

Typed fact-id inputs **are** threaded: `score.recompute` records one `derivation_inputs` row per contributing fact with a real `fact_record_id` FK, so a score resolves to the exact fact versions it read — the last hop of "traceable back to the raw payload". Only **present** components contribute (a component omitted for an immature baseline read no fact), and sleep collapses to the authoritative provider, so a candidate that lost source selection never reads as an input. The public response is unchanged: roles only, deduplicated, since a repeated role would leak the day's fact count.

### `/v1/connections/{provider}` — link a wearable

Two authenticated legs drive an OAuth link. The tenant comes from the session, never a parameter, so a caller can only link a connection for their own tenant.

```bash
# 1. begin: returns the provider authorize URL to redirect the browser to
curl --cookie akunaki_session=<token> \
  localhost:8000/v1/connections/polar/authorize
# 2. the provider redirects back with ?state=&code=; complete the link:
curl --cookie akunaki_session=<token> \
  'localhost:8000/v1/connections/polar/callback?state=...&code=...'
```

Per-provider OAuth credentials come from config (`AKUNAKI_{PROVIDER}_CLIENT_ID` / `_CLIENT_SECRET` / `_REDIRECT_URI`). A provider that is **not fully configured**, or unknown, returns an indistinguishable **404** — an unconfigured deployment reveals nothing about which providers *could* be linked. `build_oauth_client` maps a provider to its concrete client (`oura`/`polar`/`google_health`), so the linking service stays provider-uniform. A transient token-exchange failure is a `503`; a permanent one (`invalid_grant`) a `400` that should drive re-consent.

### `GET /v1/connections` — is my data actually flowing?

Lists the caller's connections with per-connection sync status: provider, lifecycle status, last successful sync, failure streak, and ingest counts. Tenant from the session, so a caller sees only their own.

```bash
curl --cookie akunaki_session=<token> localhost:8000/v1/connections
```

Carries **no health values** — statuses, timestamps, and counts only — and `last_error_class` is an error *class*, never a vendor body, so a failing connector cannot leak payload contents into a user-facing surface. Counts are scoped to their own connection: a tenant with two providers sees each one's real ingest volume, not the tenant-wide total.

### `POST /v1/connections/{id}/sync` — sync now

Enqueues an immediate incremental sync. Authenticated, CSRF-enforced, and requires an `Idempotency-Key`.

```bash
curl -X POST --cookie akunaki_session=<token> \
  -H 'X-Akunaki-CSRF: <secret>' -H 'Idempotency-Key: <key>' \
  localhost:8000/v1/connections/<connection_id>/sync
```

It queues the **same** `connection.incremental_sync` job the webhook and reconcile paths use, so a manual sync has no separate semantics: it resumes from the stored cursor and dedupes on content hash like any other. The key is namespaced per connection, so a double-clicked button queues one job (`created: false` on the repeat). A `needs_reauth` or `revoked` connection is a **409** rather than a job doomed to burn attempts; unknown and cross-tenant are an indistinguishable **404** that queues nothing.

### `GET /v1/anomalies` — active and recently-cleared flags

Anomalies are deterministic, **non-diagnostic** wellness flags: one metric departed far from the user's own recent baseline. Nothing here names a condition or advises treatment.

```bash
curl --cookie akunaki_session=<token> \
  'localhost:8000/v1/anomalies?day=2026-07-25&window_days=14'
```

`/v1/today` reduces these to one boolean that floors the training label — enough to change a recommendation, not to explain it. This surface discloses the intervals themselves: feature code, severity, start/end days, detector version. Cleared intervals stay listed for a bounded window (default 14 days, capped at 90) because an anomaly that opened Tuesday and cleared Thursday explains a past day; an interval **still open** is always listed regardless of the window. The detector's internal `z_like` is **never disclosed** — a bare z against a private baseline invites exactly the over-reading the non-diagnostic framing avoids. `day` is required, like every day surface: the server never guesses a tenant's local health day from its own clock.

### `GET /v1/workouts` — sessions and detail

```bash
curl --cookie akunaki_session=<token> 'localhost:8000/v1/workouts?limit=20'
curl --cookie akunaki_session=<token> localhost:8000/v1/workouts/<workout_id>
```

Discloses what was **measured** (start/end, per-zone minutes) plus the internally computed `session_load`. There is deliberately **no workout score**: v0.1.0 ships exactly one score code (recovery), and a second would imply a formula that has not been accepted. A row flagged `exclude_from_load = 1` is hidden from the list too, not just from load math — that flag marks a *duplicate* of the same real session from a second provider, so showing it would present one workout twice. Pagination is **keyset** on `(start_utc, id)` with an opaque cursor, not offset: workouts arrive continuously, and an offset would skip or repeat rows when a sync lands between pages.

### `POST /v1/privacy/delete` — irreversible erasure

```bash
curl -X POST --cookie akunaki_session=<token> \
  -H 'X-Akunaki-CSRF: <secret>' localhost:8000/v1/privacy/delete
curl localhost:8000/v1/privacy/delete/<deletion_request_id>   # status
```

Runs the full pipeline **synchronously**, so a `200` means the data is gone — not queued. Tenant from the session: a caller can only erase their own. The response clears the session cookie, since every session cascades away with its tenant, and carries counts only. The status read is **unauthenticated by necessity** — the tenant and all its sessions are gone by the time a completed request is worth reading — which is safe because the id is an unguessable UUID and the response discloses only a pipeline status; unknown and cross-tenant are the same 404.

### `/webhooks/{provider}/{connection_id}` — push-triggered refetch

`POST /webhooks/{provider}/{connection_id}` is **unauthenticated** — the vendor calls it, not a browser — so all trust comes from the **HMAC-SHA256 signature** over the exact request body, verified in constant time against `AKUNAKI_{PROVIDER}_WEBHOOK_SECRET`. On success the delivery is recorded once in a **deduplicated inbox** (`(connection_id, dedupe_key)` unique; dedupe on the vendor delivery id or a body hash) and a `connection.incremental_sync` refetch is enqueued (idempotency-keyed, so concurrent deliveries collapse). The response is a fast `{"status": "accepted"}` (or `"duplicate"` on a vendor redelivery) — **never the fetched data**; the body is only a trigger, never trusted as data.

A bad signature, tampered body, or unknown connection is an indistinguishable **401** (no cross-connection probing); an unconfigured provider is a **404**. Oura and Polar verify an HMAC-SHA256 body signature (`AKUNAKI_{PROVIDER}_WEBHOOK_SECRET`). **Google Health** verifies its Google-signed push OIDC token instead: the Bearer JWT in the `Authorization` header is checked against Google's rotating **JWKS** (asymmetric algorithms only — an HS256 downgrade is rejected), then its issuer, audience (`AKUNAKI_GOOGLE_HEALTH_PUSH_AUDIENCE`), expiry, and push **service account** (`AKUNAKI_GOOGLE_HEALTH_PUSH_SERVICE_ACCOUNT`, with `email_verified`) — the "endpoint authorization" that proves the push came from *our* subscription's identity.

### Local driver limitation: BLOB binding

`libsql_experimental` stores BLOBs correctly but exposes no DBAPI `Binary` constructor, so SQLAlchemy's stock `LargeBinary` raises in its bind processor before executing. Binary columns therefore use `akunaki.adapters.db.types.Blob`, a `TypeDecorator` that passes `bytes` straight through. DDL is still `BLOB`. See note 4 in [phase-zero-turso-foundation.md](../docs/evidence/phase-zero-turso-foundation.md).

## Configuration

All settings use the **`AKUNAKI_`** prefix (pydantic-settings).

| Variable | Default | Notes |
|----------|---------|-------|
| `AKUNAKI_DATABASE_URL` | `sqlite+libsql:///.local/akunaki.db` | Local `sqlite+libsql` only: official in-memory (`sqlite+libsql://`), path in-memory, relative file, or absolute file. Hostnames, credentials, ports, query strings, and fragments are rejected. Parent dirs for file URLs are created on engine build. |
| `AKUNAKI_SERVICE_NAME` | `akunaki-api` | Reported by `/healthz` |
| `AKUNAKI_ECHO_SQL` | `false` | Dev SQL echo |
| `AKUNAKI_SECRET_KEKS` | *(empty)* | Envelope-encryption KEKs as `version:base64key` pairs, comma separated; each key must decode to exactly 32 bytes. Empty means secret sealing is unavailable and any process that needs it fails fast. |
| `AKUNAKI_OIDC_ISSUER` | *(empty)* | OIDC issuer URL. Empty means the login routes are not mounted (no auth surface). |
| `AKUNAKI_OIDC_CLIENT_ID` / `AKUNAKI_OIDC_CLIENT_SECRET` | *(empty)* | OIDC client credentials from the IdP. |
| `AKUNAKI_OIDC_REDIRECT_URI` | *(empty)* | Exact callback URI registered with the IdP; must match at the callback. |
| `AKUNAKI_SESSION_COOKIE_SECURE` | `true` | `Secure` attribute on the session cookie; only disable for local HTTP development. |
| `AKUNAKI_ACTIVE_KEK_VERSION` | *(empty)* | KEK version new envelopes are sealed under. Optional when exactly one KEK is configured; **required** when several are. |

### Secret sealing (envelope encryption)

Provider tokens are stored only as envelope-encrypted ciphertext:

```python
sealer = build_sealer(get_settings())          # fails fast if no KEK configured
sealed = sealer.seal(token_bytes, aad=b"conn-1")
# persist sealed.ciphertext + sealed.key_version
plaintext = sealer.open(sealed, aad=b"conn-1")
```

Each `seal` draws a fresh AES-256 DEK and fresh nonces; the DEK is wrapped by the active KEK. `aad` binds an envelope to its owning row, so ciphertext copied onto a different connection will not open. Rotation is additive: keep old KEK versions in `AKUNAKI_SECRET_KEKS` so existing rows stay readable while new writes use the new active version.

**Never commit real keys.** Generate a local development key with:

```bash
uv run python -c "import base64,secrets;print('dev-v1:'+base64.b64encode(secrets.token_bytes(32)).decode())"
```

Production KEKs belong in the platform secret store or a KMS; see [phase-zero-envelope-encryption.md](../docs/evidence/phase-zero-envelope-encryption.md) for what is and is not covered.

### OAuth state and PKCE

`OAuthStateRepository` holds the callback-security rules so no call site can skip one:

```python
state, verifier = generate_state(), generate_code_verifier()
challenge = code_challenge_s256(verifier)          # goes on the authorize URL
repo.create(
    state_id="s1", tenant_id="t1", provider="oura", state=state,
    sealed_verifier=sealer.seal(verifier.encode(), aad=b"s1"),
    redirect_uri=REDIRECT, now=now, ttl=timedelta(minutes=10),
)
# ... user returns ...
result = repo.consume(state=state, redirect_uri=REDIRECT, now=now)
if result.ok:
    verifier = sealer.open(result.sealed_verifier, aad=b"s1").decode()
```

The raw `state` is **never stored** — only its SHA-256 hash — and the PKCE verifier is stored sealed. `consume` enforces single use (atomic `UPDATE ... WHERE consumed_at IS NULL`), expiry, and an **exact** redirect-URI match, returning a typed `rejection` instead of raising so callers can surface one generic error without revealing which check failed. A failed attempt does not burn the state. Spent rows and their sealed verifiers are dropped by the scheduled retention sweep (below).

PKCE is **S256** only; `plain` is deliberately unsupported.

### Linking a provider

`OAuthLinkingService` wires the client, state repository, and sealer into one flow:

```python
redirect = service.start_link(tenant_id=..., redirect_uri=REDIRECT,
                              scopes=("daily", "personal"), now=now)
# send the user to redirect.authorize_url ...
result = service.complete_link(state=state, code=code,
                               redirect_uri=REDIRECT, now=now)
result.ok            # LinkedConnection, or a typed LinkRejection
```

The connection row and its sealed tokens are written in **one transaction**, so an `active` connection always has usable token material — a failed exchange leaves nothing behind. Re-consent reuses the existing `(tenant_id, provider)` row rather than creating a duplicate. `LinkRejection.PROVIDER_REJECTED` (from `invalid_grant`) is **not** retryable and should drive `needs_reauth`; `PROVIDER_UNAVAILABLE` is.

**HTTP routes are deliberately not implemented yet.** `/v1` endpoints need a `tenant_id` from an authenticated session, and auth/OIDC is not built. `tenant_id` is a service parameter, so the routes become a thin layer once sessions exist.

### Oura OAuth client

`OuraOAuthClient` builds the authorize URL and performs the PKCE token exchange:

```python
client = OuraOAuthClient(client_id=..., client_secret=...)
url = client.authorize_url(state=state, code_challenge=challenge,
                           redirect_uri=REDIRECT, scopes=("daily", "personal"))
result = client.exchange_code(code=code, code_verifier=verifier,
                              redirect_uri=REDIRECT, now=now)
if result.ok:
    sealer.seal(result.tokens.access_token.encode(), aad=connection_id.encode())
```

Failures map to a typed vocabulary rather than raising: `invalid_grant` / `invalid_client` are **not retryable** and must drive `needs_reauth`, while 5xx and transport errors are retryable (`TokenExchangeFailure.retryable`). Provider response bodies are **never** logged or attached to exceptions — a token endpoint body contains credentials — and both `OuraOAuthClient` and `OAuthTokens` have redacted `__repr__`s. Relative `expires_in` is converted to an absolute `expires_at` so it stays meaningful across restarts.

### Polar OAuth client

`PolarOAuthClient` mirrors the Oura client with Polar AccessLink's differences: the token exchange authenticates via **HTTP Basic** (`client_id`/`client_secret` in the `Authorization` header, never a form field), there is **no PKCE** (`authorize_url` takes only `state`), and there is **no refresh token** — an AccessLink access token is long-lived, so there is no `refresh`. The token body's `x_user_id` is captured as `OAuthTokens.external_user_id` and flows into the connection's `external_user_id`. The same typed failure vocabulary and secret-leak discipline (redacted repr, no body logging) apply. The linking service now handles this non-PKCE flow (via the client port's `uses_pkce` flag), so a full Polar authorize→callback link works end to end; the connector link routes at `/v1/connections/{provider}` now drive it end to end.

### Google Health OAuth client

`GoogleHealthOAuthClient` is closest to the Oura client — standard Google OAuth2 authorization-code + **PKCE (S256)**, with `client_secret` in the token-request **form** (a confidential client, like Oura, unlike Polar's Basic auth) and a **refresh token**. Its one Google-specific detail: the authorize URL adds `access_type=offline` and `prompt=consent`, both required for Google to return a refresh token — without them a connection could never refresh. A refresh response omits `refresh_token`, so the stored one stays in force. Same typed failure vocabulary and secret-leak discipline as the other clients. The linking service already handles this PKCE flow; the connector link routes at `/v1/connections/{provider}` now drive it end to end.

There is **no** `AKUNAKI_DATABASE_AUTH_TOKEN` and **no** remote connect-args path in this foundation.

### Accepted `AKUNAKI_DATABASE_URL` forms

| Form | Example |
|------|---------|
| Official in-memory | `sqlite+libsql://` |
| Path in-memory | `sqlite+libsql:///:memory:` |
| Relative file | `sqlite+libsql:///.local/akunaki.db` |
| Absolute file | `sqlite+libsql:////abs/path/to/file.db` |

Remote host URLs (including Turso Cloud hosts), credentialed URLs, non-`sqlite+libsql` dialects, and **any** query string or fragment (including `authToken`, `syncUrl`, `secure`, or arbitrary parameters) are rejected at settings validation.

## Layout

```text
src/akunaki/
  domain/           # pure job/retry/secret types + sleep normalizer (no SQLAlchemy)
  application/      # worker runtime + handler registry (port-typed, no SQLAlchemy)
  ports/            # JobRepositoryPort + SecretSealerPort protocols
  adapters/db/      # engine, models, JobRepository CAS adapter
  adapters/crypto/  # AES-256-GCM envelope sealer, KEK config, OAuth state/PKCE
  adapters/connectors/ # provider OAuth + fetch clients (Oura)
  application/      # + OAuthLinkingService, InitialSyncHandler
  api/              # FastAPI app factory + /healthz
  worker/           # core worker entrypoint: claim loop + signal shutdown
alembic/            # migrations 0001 foundation + 0002 leases + 0003 execution lifecycle
tests/              # temp-file libSQL tests (no leftover artifacts)
```

## Dependency policy

- Prefer **latest stable** only; never pin prereleases for production path.
- Dev HTTP client for Starlette/FastAPI `TestClient` is **`httpx2==2.5.0`** (Starlette 1.3.1 prefers httpx2; plain `httpx` is deprecated for that path).
- **pydantic 2.13.4** is the latest stable top-level Pydantic release as of **2026-07-13**. **pydantic-core** is a separate internal package with an independent version sequence; Pydantic 2.13.4 requires **pydantic-core 2.46.4** exactly. Therefore **2.13 versus 2.46 is not an age comparison**, and **core 2.47.0 must not be forced**. Do not change the Pydantic pin. An outdated `pydantic-core` line from `uv tree --outdated` is expected under that constraint.
- Re-run `uv tree --outdated` and `uv run pip-audit` when refreshing pins.
- Do not add model provider packages to the core dependency set.
- Pytest is configured with `filterwarnings = ["error"]` so new warnings fail the suite.

## Evidence

See `docs/evidence/phase-zero-turso-foundation.md`, `docs/evidence/phase-zero-job-concurrency.md`, and `docs/implementation-status.md` at the repository root.
