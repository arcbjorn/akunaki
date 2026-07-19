# Akunaki backend (Phase Zero foundation)

Model-free **FastAPI + SQLAlchemy 2 + sqlalchemy-libsql + Alembic** foundation.

This package intentionally includes **no** frontend, auth product surface, or model/AI SDKs. Full product schema remains **pending**.

Implemented: the **local** atomic durable-job repository lifecycle (fenced claims with attempt history; transactional completion, retry scheduling, dead-lettering, and lease expiry), the **worker runtime** with retry/backoff policy, **idempotent enqueue**, **envelope encryption** for secret columns, the **OAuth state/PKCE handshake primitives**, the **Oura OAuth client** (authorize URL, PKCE code exchange, refresh), the **OAuth linking service**, the **`connection.initial_sync` handler** with the Oura V2 fetch client and atomic ingestion commit, and the **Oura sleep normalizer** writing versioned canonical facts. Not implemented: the `raw.normalize` job handler, other detail tables, source selection and scoring, HTTP authorize/callback routes (deferred pending auth), webhooks, incremental sync, and the Google Health / Polar connectors.

**Implemented storage scope:** local **libSQL / Turso-compatible** `sqlite+libsql` only (in-memory or file). **Turso Cloud / remote** is intentionally deferred by product decision — not wired in this foundation and **not** blocked on credentials. Long-term production Turso architecture remains documented under `docs/` as proposed future context (ADR 0003, architecture pages).

## Requirements

| Item | Policy |
|------|--------|
| Python | **3.13.14** only (`requires-python = ">=3.13.14,<3.14"`) |
| Dependencies | **Exact pins** of latest **stable** releases as of 2026-07-13 (`cryptography==49.0.0` added 2026-07-18; `httpx2==2.6.0` promoted to runtime 2026-07-19) — **no prereleases** |
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
uv run alembic downgrade 20260719_0006   # drop sleep fact schema
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

**Known placeholder:** pages are keyed as `stream:page:<content_hash>` rather than by real vendor record ids, because Oura returns collection pages and no per-record normalizer exists yet. This is safe for dedupe (an unchanged page appends no new revision) but means one revision currently represents a page, not a record. Real per-record identity arrives with the normalizer.

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
