# Repository and services

**Status:** Proposed

**Last reviewed:** 2026-07-13

Authoritative for **service boundaries**, **repository structure**, and **job system** design (coverage matrix items 1, 2, 13). This is a proposed layout for a greenfield implementation; no packages exist yet.

---

## Target monorepo layout

```text
akunaki/
  backend/                 # Python package + process entrypoints
    pyproject.toml         # core deps; optional extras: [agent], [mcp]
    alembic/
    src/akunaki/
      domain/              # pure deterministic core (no model SDKs)
      application/         # use cases, tool registry
      ports/               # protocols (incl. optional model port)
      adapters/
        db/                # SQLAlchemy / libSQL / Turso
        connectors/
        models/            # only installed with [agent] extra
        crypto/
      api/                 # FastAPI (core; no model SDK import at boot)
      worker/              # core scheduler + claim loop
      agent_worker/        # optional phase-four process
      mcp_adapter/         # optional phase-four+ process
    tests/
  web/                     # Next.js TypeScript PWA
    package.json
    src/
    tests/
  infra/                   # IaC, container defs, deploy manifests
  docs/                    # this documentation set (present now)
```

### Dependency rules

| From | May depend on | Must not depend on |
|------|---------------|--------------------|
| `domain` | stdlib, pure typing libs | SQLAlchemy, HTTP, FastAPI, Next, env I/O, model SDKs |
| `application` | domain, ports | FastAPI routers, connector HTTP clients, concrete LLM SDKs |
| `ports` | domain types only | adapters |
| `adapters.*` | ports, domain types, vendor SDKs | `api` routes, worker loop internals |
| `adapters.models.*` | ports, optional model SDKs | core install path; never imported by API/worker boot without extra |
| `api` | application, adapters wiring (core) | domain formulas inlined in routes; required model SDK |
| `worker` (core) | application, adapters wiring (core) | UI, model prompt prose as business rules; model SDKs |
| `agent_worker` | application, model adapters (optional extra) | core product paths; must remain optional |
| `mcp_adapter` | application / tool registry | second business layer |
| `web` | product API contracts (`/v1`) | Python packages, SQL, vendor OAuth secrets |

**Application services own use cases.** The **tool registry** is a typed capability facade over **selected** services, built in **phase two independently of AI**. Matching REST, report, agent, and MCP adapters reuse it. OAuth/lifecycle routes may call the same application services **directly** when a tool wrapper is unnecessary.

### No-model structural guarantee

| Rule | Detail |
|------|--------|
| Core install | `pip install` / image **without** `[agent]` has **no** model SDK packages |
| API / core-worker boot | Succeeds with **no** model config; model ports unbound or null adapters |
| Product path | Ingestion, engine, recommendations, notifications, dashboard, export never call models |
| Agent isolation | Missing or crashed **agent-worker** cannot degrade core paths |
| MCP isolation | Optional process; outage does not affect core product |

---

## Service boundaries (modular monolith)

MVP is **one deployable backend codebase** with multiple process roles and one web frontend.

| Boundary | Process | Public surface | Private surface |
|----------|---------|----------------|-----------------|
| **Web PWA** | Node (Next.js) | User-facing UI | Session cookie relay / BFF only as needed |
| **Product API** | FastAPI | REST `/v1`, SSE, provider webhooks | Application services; enqueues agent runs; streams persisted events |
| **Core worker** | Python process | None (internal) | Sync, normalize, recompute, export, delete, notifications |
| **Agent-worker** | Optional Python process | None (internal) | Claims agent jobs only; model I/O; tool execution via registry |
| **MCP adapter** | Optional process | stdio / Streamable HTTP | Same tools/services; not a second domain |
| **Operational DB** | **Turso** (prod); local libSQL/SQLite (dev) | Shared by API + workers | Schema via Alembic |

There is **no** separate microservice per connector in MVP. Connectors are modules behind a typed port inside the same package.

### Shared package, multiple entrypoints

```text
python -m akunaki.api            # required API entry
python -m akunaki.worker         # required core worker entry
python -m akunaki.agent_worker   # optional phase four
python -m akunaki.mcp_adapter    # optional phase four+
```

API and core worker bind the same core composition root: config, DB engine, connector registry, tool registry (phase two+), job repository. Agent-worker binds model adapters only when the optional extra and config are present. MCP binds a thin transport over the tool registry.

---

## Technology choices (target)

| Layer | Choice | Notes |
|-------|--------|-------|
| Web | Next.js, TypeScript, PWA | Mobile-first responsive |
| API | FastAPI, Python 3.12+ | Versioned REST base **`/v1`** |
| Core worker | Same Python package | Scheduler tick + claim loop |
| Agent-worker | Same package, optional extra | Separately deployable; phase four |
| ORM / migrations | SQLAlchemy 2 + Alembic | Phase zero validates exact Turso path |
| Operational DB | **Turso** selected for prod; SQLite/libSQL local | No DuckDB in MVP; [ADR 0003](../adr/0003-libsql-operational-store.md) |
| Job queue | Durable tables in operational store | One active core worker initially |
| Crypto | Envelope encryption for secrets; encryption at rest for DB/backups | See [security.md](security.md) |
| Vectors | Deferred optional boundary on Turso | Not MVP schema; later-ready agent only |

ADR: [../adr/0001-modular-monolith.md](../adr/0001-modular-monolith.md), [../adr/0003-libsql-operational-store.md](../adr/0003-libsql-operational-store.md).

---

## Job system

### Design goals

- At-least-once execution with **idempotent** handlers
- **Atomic claim** with lease expiry and **fencing tokens**
- Retries with backoff, **dead letters**, and poison-message isolation
- Tenant-scoped work; privacy deletion cancels related jobs before hard-scrub; completion proof + restoration-suppression ledger per [security.md](security.md)
- Single active **core** worker until scale trigger fires
- **Agent jobs** claimed only by agent-worker; core worker never requires model availability

### Proposed tables (summary)

Full columns in [data-model.md](data-model.md). Logical groups:

| Table | Purpose |
|-------|---------|
| `jobs` | Work units: type, payload, status, priority, run_after, attempts |
| `job_leases` | Active lease owner, leased_until, fence_token |
| `job_attempts` | Attempt history, error class, redacted message |
| `job_dead_letters` | Exhausted jobs for operator review |
| `idempotency_keys` | API and internal mutation dedupe |

### Job types (MVP + phase four)

| Type | Trigger | Claimed by | Idempotency key sketch |
|------|---------|------------|------------------------|
| `connection.initial_sync` | OAuth success | core worker | `tenant:connection:initial` |
| `connection.incremental_sync` | Schedule / webhook | core worker | `tenant:connection:cursor_or_window` |
| `raw.normalize` | New raw revision | core worker | `tenant:raw_revision_id` |
| `day.recompute` | Affected dates | core worker | `tenant:local_date:formula_version` |
| `export.create` | User request | core worker | client `Idempotency-Key` |
| `privacy.delete` | User request | core worker | `tenant:delete:request_id` |
| `report.scheduled` | Cron expression | core worker | `tenant:report:period` |
| `agent.run` | User message / continuation | **agent-worker only** | `tenant:conversation:run_id` |

### Claim protocol (proposed)

SQLite / libSQL / Turso **do not** provide portable `SELECT FOR UPDATE` / `SKIP LOCKED`. Atomic claim is implemented with **candidate discovery + conditional compare-and-swap**, not row-level locking idioms from Postgres.

1. **Discover candidates** (non-locking read): select job ids where `status=ready` and `run_after <= now`, ordered by priority and created_at, filtered by **worker role** (core vs agent). Read expected `fence_token` (or row version) with each candidate.
2. **Claim attempt (short transaction):** for one candidate, run a conditional `UPDATE` that succeeds only if still `status=ready`, `run_after <= now`, and `fence_token` (or version) equals the expected value; set `status=leased`. Use `RETURNING` and/or affected-row count: **0 rows → lose; retry next candidate**. Do **not** rely on `SELECT FOR UPDATE` / `SKIP LOCKED`.
3. **On win (same short transaction or immediately after):** insert/update `job_leases` with `leased_until = now + lease_ttl`, increment fence token for this lease generation. Commit.
4. Losers discard the candidate and try another (or re-discover). Concurrent workers may race; only one CAS wins.
5. Handler runs out of band with lease heartbeat extension for long syncs.
6. On success: `status=succeeded`, release lease, enqueue dependents.
7. On retryable failure: increment attempts, compute backoff `run_after`, `status=ready` if under max else `dead_letter`.
8. On commit of side effects, handlers check fence token still matches; stale fence aborts write.

### Passive standby and leader fencing

Optional **passive standby** for the core worker must not schedule ticks or reap leases until it holds a **leader lease** with fencing:

1. Standby attempts CAS claim of a single-row **leader lease** (or equivalent coordination row) with expiry + fence token—same conditional-UPDATE pattern as job claim (no `FOR UPDATE` dependency).
2. Only the leader schedules cron enqueues and runs the lease reaper.
3. On leader loss or fence mismatch, stop scheduling/reaping immediately; the promoted standby increments fence so a stale former leader cannot enqueue or requeue.

### Retry policy (default)

| Class | Max attempts | Backoff |
|-------|--------------|---------|
| Rate limit (429) | 8 | Honor `Retry-After` when present; else exponential 30s–30m jitter |
| Transient network | 6 | Exponential 10s–10m jitter |
| Auth revoked | 1 | Dead letter + connection health `needs_reauth` |
| Validation / schema | 1 | Dead letter + operator signal |
| Worker crash mid-lease | n/a | Lease expiry returns job to `ready` |
| Agent-worker missing | n/a | Agent jobs remain queued/failed; **core product unaffected** |

### Scale trigger (before Redis or external broker)

Begin evaluating an external broker or multi-worker design when **any sustained** SLO/resource/contention trigger holds (not only when all hold simultaneously)—for two consecutive weeks of production traffic, or sooner if a single class is critically breached:

1. p95 job queue wait time exceeds 5 minutes for user-visible sync/recompute classes, **or**
2. Single worker CPU or connector concurrency is saturated after in-process concurrency tuning, **or**
3. Lease contention or DB write amplification from job tables exceeds agreed SLO budget in [operations.md](operations.md).

Until then: one active core worker, optional in-process concurrent handlers with a global concurrency cap per provider. Agent-worker may scale independently only for agent job classes and never unblocks core SLOs by coupling.

---

## Scheduler

Proposed responsibilities of the **core** worker process:

- Cron-like tick every N seconds (config) to enqueue due incremental syncs and scheduled reports
- Connection health probes (token expiry windows)
- Lease reaper: expire leases, requeue orphaned **core** jobs
- Dead-letter metrics emission (PHI-free)
- Core worker health self-check; optional passive standby for core worker **only after leader lease/fence is held** (see claim protocol above and [operations.md](operations.md))

The API process does **not** run heavy sync work; it may enqueue jobs only (including `agent.run` when the agent is enabled).

The **agent-worker** claims only agent job types, heartbeats its own leases, and never runs connector fetch or recompute.

---

## Configuration surface (proposed)

| Variable group | Examples | Held by |
|----------------|----------|---------|
| `DATABASE_URL` | Turso / libSQL / SQLite URL | API + all workers |
| `SESSION_*` | cookie keys, TTL | API |
| `OIDC_*` | issuer, client | API |
| `OAUTH_*_CLIENT_*` | per provider | API + core worker |
| `ENCRYPTION_KEK_*` | envelope KEK ref | API + workers |
| `MODEL_*` | optional providers | **agent-worker** (+ API for config CRUD only) |
| `AGENT_WORKER_*` | lease TTL, concurrency | agent-worker |
| `WORKER_*` | lease TTL, concurrency | core worker |
| `PUBLIC_WEB_ORIGIN` | CORS / CSRF | API |
| `EXPORT_OBJECT_STORE_*` | private encrypted export objects | API + core worker |

Secrets never in the frontend bundle. Full threat model: [security.md](security.md). Config ops: [operations.md](operations.md).

Core API/worker boot **must not** require `MODEL_*` variables.

---

## Interface contracts between processes

| From → To | Contract |
|-----------|----------|
| Web → API | Versioned REST **`/v1/*`**, SSE **`/v1/conversations/{id}/events`** |
| Vendor → API | Provider-specific signed webhooks + replay protection (not generic HMAC-only) |
| API → DB | SQLAlchemy sessions; short transactions |
| Core worker → DB | Same schema; longer transactions only for claim/complete |
| Agent-worker → DB | Same schema; agent jobs, conversation events, tool audit |
| Core worker → Vendor | Connector HTTP; rate-limit aware |
| API → Agent path | Persist events + enqueue `agent.run`; no inline model generation on critical request path |
| Agent-worker → Models | Optional async provider port; never on scoring path |
| MCP → Tools | Same registry → application services |

---

## Related

- [overview.md](overview.md)
- [data-model.md](data-model.md)
- [api-tools-and-agent.md](api-tools-and-agent.md)
- [operations.md](operations.md)
- [../adr/0001-modular-monolith.md](../adr/0001-modular-monolith.md)
- [../adr/0003-libsql-operational-store.md](../adr/0003-libsql-operational-store.md)
