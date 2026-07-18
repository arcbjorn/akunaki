# Akunaki backend (Phase Zero foundation)

Model-free **FastAPI + SQLAlchemy 2 + sqlalchemy-libsql + Alembic** foundation.

This package intentionally includes **no** frontend, connectors, auth product surface, or model/AI SDKs. Full product schema remains **pending**. The **local** atomic durable-job repository lifecycle is implemented: fenced claims create attempt history, and completion, retry scheduling, dead-lettering, and lease-expiry transitions are transactional. The **worker runtime**, retry/backoff policy, and job handlers are **not** implemented.

**Implemented storage scope:** local **libSQL / Turso-compatible** `sqlite+libsql` only (in-memory or file). **Turso Cloud / remote** is intentionally deferred by product decision — not wired in this foundation and **not** blocked on credentials. Long-term production Turso architecture remains documented under `docs/` as proposed future context (ADR 0003, architecture pages).

## Requirements

| Item | Policy |
|------|--------|
| Python | **3.13.14** only (`requires-python = ">=3.13.14,<3.14"`) |
| Dependencies | **Exact pins** of latest **stable** releases as of 2026-07-13 — **no prereleases** |
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

Execution policy lives in `akunaki.application.worker_runtime` (port-typed, no SQLAlchemy); durability lives in `JobRepository`. Handlers register in `akunaki.application.handlers`; only `system.noop` ships today. Handlers **must be idempotent** — a lease can expire mid-run and the job be retried elsewhere.

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
uv run alembic downgrade 20260713_0003   # drop connection lifecycle schema
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

### Local driver limitation: BLOB binding

`libsql_experimental` stores BLOBs correctly but exposes no DBAPI `Binary` constructor, so SQLAlchemy's stock `LargeBinary` raises in its bind processor before executing. Binary columns therefore use `akunaki.adapters.db.types.Blob`, a `TypeDecorator` that passes `bytes` straight through. DDL is still `BLOB`. See note 4 in [phase-zero-turso-foundation.md](../docs/evidence/phase-zero-turso-foundation.md).

## Configuration

All settings use the **`AKUNAKI_`** prefix (pydantic-settings).

| Variable | Default | Notes |
|----------|---------|-------|
| `AKUNAKI_DATABASE_URL` | `sqlite+libsql:///.local/akunaki.db` | Local `sqlite+libsql` only: official in-memory (`sqlite+libsql://`), path in-memory, relative file, or absolute file. Hostnames, credentials, ports, query strings, and fragments are rejected. Parent dirs for file URLs are created on engine build. |
| `AKUNAKI_SERVICE_NAME` | `akunaki-api` | Reported by `/healthz` |
| `AKUNAKI_ECHO_SQL` | `false` | Dev SQL echo |

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
  domain/           # pure job lifecycle/concurrency types + retry policy (no SQLAlchemy)
  application/      # worker runtime + handler registry (port-typed, no SQLAlchemy)
  ports/            # JobRepositoryPort protocol
  adapters/db/      # engine, models, JobRepository CAS adapter
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
