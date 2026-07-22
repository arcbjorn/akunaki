# Akunaki backend (Phase Zero foundation)

Model-free **FastAPI + SQLAlchemy 2 + sqlalchemy-libsql + Alembic** foundation.

This package intentionally includes **no** frontend, auth product surface, or model/AI SDKs. Full product schema remains **pending**.

Implemented: the **local** atomic durable-job repository lifecycle (fenced claims with attempt history; transactional completion, retry scheduling, dead-lettering, and lease expiry), the **worker runtime** with retry/backoff policy, **idempotent enqueue**, **envelope encryption** for secret columns, the **OAuth state/PKCE handshake primitives**, the **Oura OAuth client** (authorize URL, PKCE code exchange, refresh), the **OAuth linking service**, the **`connection.initial_sync` handler** with the Oura V2 fetch client and atomic ingestion commit, the **Oura sleep normalizer** writing versioned canonical facts, the **OIDC login flow** with hash-only opaque sessions, the authenticated **`/v1/sleep` deterministic summary** (adherence + 14-day debt, a summary not a score), the authenticated **`/v1/recovery` surface** running the full `general_recovery_v0.1.0` scoring path (a real score once overnight HRV/RHR ingest, else honestly `insufficient`), the **overnight-vitals ingestion** (HRV/RHR/temperature/respiratory from the Oura sleep payload), the composite **`/v1/today`** view stitching recovery and sleep, **versioned score persistence** (`daily_health_scores`/`score_factors`), and the **`score.recompute` handler chained after `raw.normalize`** so scores recompute automatically as data lands. the authenticated **`POST /v1/checkin`** write path feeding the subjective component, and recovery/today surfaces that **serve the persisted score** (disclosing its version and freshness), falling back to compute-on-read only for a day never scored. All nine recovery components have their formulas and can activate from real data (prior-load from `workout_sessions`). The deterministic **anomaly detectors + persistence** (open/2-day-clear intervals, detected automatically during `score.recompute`), **training label**, and **recommendation rules** (Stage 4/5) are implemented; the training label + primary/supporting recommendations ship on `/v1/today`, and a persisted high-severity anomaly floors the label at `light`. The **typed tool registry** (AI-independent) is exposed over `/v1/tools`. **Canonical zone-load** and the **Polar workout normalizer** activate the prior-load/ACWR path from `workout_sessions`, flowing through the ingestion loop via schema-version dispatch. The **Polar fetch client** (AccessLink exercises) ships alongside the workout normalizer. Not implemented: a Polar sync config + OAuth token exchange (to pull workouts from a live connection), the activity anomaly (no activity ingestion), the strain/activity blocks, source selection, HTTP authorize/callback routes (deferred pending auth), webhooks, incremental sync, and the Google Health connector.

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
# GET http://127.0.0.1:8000/healthz
```

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

Only the holder of the `core-reaper` **leader lease** requeues expired leases and dead-letters exhausted ones, so a passive standby never reaps behind an active worker.

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

### Internal debug surface

Phase one's vertical slice — "see raw sync success and latest sleep fact in API" — is served by an **internal, unauthenticated** router:

```bash
AKUNAKI_DEBUG_ROUTES_ENABLED=true uv run python -m akunaki.api
curl 'localhost:8000/internal/debug/sync-status?tenant_id=t1'
curl 'localhost:8000/internal/debug/latest-sleep?tenant_id=t1'
```

**Off by default and fails closed**: with the flag unset the routes are not registered at all, so they are absent from the OpenAPI schema rather than merely guarded at request time. Responses carry `private, no-store`, and a cross-tenant read is a `404` — indistinguishable from "no data yet".

This is a deliberate stand-in for the authenticated `/v1` surface, which needs sessions. It should be **replaced**, not extended.

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

The composite owns no formula: `akunaki.application.today_surface` delegates to the recovery and sleep surface services and combines their disclosures, then applies the pure training-label and recommendation rules. A persisted **active high-severity anomaly floors the training label at `light`** (read via `AnomalyRepository`). ACWR is not yet threaded (no load source), so the load rules cannot fire. Verified end to end, including that unshipped blocks never appear as fabricated data.

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

Each tool is a `Tool` with Pydantic `input_model`/`output_model` and declared metadata — `scopes`, `sensitivity`, `side_effect`, `model_exposure`, `requires_confirmation`. The read-health tools (`health.get_today` / `get_recovery` / `get_sleep`) wrap the surface services and carry no formula. The **tenant comes from the tool context**, never the input, so a tool can no more cross tenants than a direct route; invoke is a state-changing POST, so CSRF is enforced. Verified end to end, including 404 for an unknown tool and 422 for a malformed argument.

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
| `AKUNAKI_DEBUG_ROUTES_ENABLED` | `false` | Mounts the **unauthenticated** internal debug router. Serves tenant health data with no session check — keep off outside local development. |
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

The raw `state` is **never stored** — only its SHA-256 hash — and the PKCE verifier is stored sealed. `consume` enforces single use (atomic `UPDATE ... WHERE consumed_at IS NULL`), expiry, and an **exact** redirect-URI match, returning a typed `rejection` instead of raising so callers can surface one generic error without revealing which check failed. A failed attempt does not burn the state. Call `purge_expired` periodically to drop spent rows and their sealed verifiers.

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
