# ADR 0001: Modular monolith

**Status:** Proposed

**Last reviewed:** 2026-07-13

## Context

Akunaki needs an API, a background worker for sync/recompute, a web PWA, and (later) optional model-agent and MCP surfaces. A greenfield team benefits from shared types, single-database transactions for jobs and domain writes, and minimal ops surface. Premature microservices would multiply deployment, auth, and consistency costs before product-market fit.

Core product value (ingestion, deterministic engine, recommendations, dashboard, export) must remain fully operable with **no model SDK**, **no required model config**, and **no agent-worker process**.

## Decision

Adopt a **modular monolith**:

- One Python package (`backend/`) shared by multiple process entrypoints from the **same** modular layout:
  - **API** (required)
  - **core worker/scheduler** (required)
  - **agent-worker** (optional, phase four; separately deployable; optional dependency extra)
  - **MCP adapter** (optional, phase four+; separately deployable adapter process)
- One Next.js TypeScript PWA
- One operational database (**Turso** in production; SQLite/libSQL local—see [0003-libsql-operational-store.md](0003-libsql-operational-store.md))
- One durable database-leased job queue with **one active core worker** initially
- Clear module boundaries (`domain`, `application`, `ports`, `adapters`) enforced by dependency rules
- **Tool registry** is a typed capability facade over selected application services, built in **phase two independently of AI**; REST, reports, agent, and MCP reuse it
- Split into separate deployable **services** only when measured triggers fire (see job scale trigger in [../architecture/repository-and-services.md](../architecture/repository-and-services.md)); optional agent-worker and MCP are process isolation within the monolith package, not a premature multi-repo split

### No-model structural guarantee

- Core install and API/core-worker startup have **no** model SDK dependency and **no** required model config.
- Phase four adds an **optional dependency extra** and a **separately deployable agent-worker**.
- API stores/streams conversation events and queues agent runs; missing or failed agent-worker **cannot** affect ingestion, engine, recommendations, notifications, dashboard, or export.
- MCP is another optional adapter process over the same tools/services.

## Consequences

### Positive

- Shared deterministic domain code without network hops
- Single Alembic chain and transactional job claims
- Simpler local development and CI
- Connectors remain modules, not fleets of services
- Agent/MCP isolation without rewriting the domain

### Negative

- Requires discipline to avoid spaghetti across modules
- Core API and core worker remain versioned together by default
- Horizontal core-worker scale needs careful lease design before multi-worker
- Optional processes still share the package; packaging extras must keep core free of model SDKs

### Neutral

- `infra/` may run multiple process types from one image with different commands and dependency sets

## Reversal conditions

Revisit if **two or more** hold:

1. Team size or release cadence makes independent deploy of connectors mandatory.
2. Job queue wait SLOs fail after in-process concurrency tuning and still fail after a well-defined multi-worker DB-lease design.
3. Regulatory or tenancy isolation requires physical separation of processing planes.
4. A subsystem (e.g. model gateway) has independently scaling cost dominance that justifies extraction beyond the optional agent-worker.

Reversal path: extract one boundary at a time (usually worker or connector fetcher) behind the existing ports, without rewriting domain formulas. Agent-worker failure is never grounds to couple models into the core path.

## Related

- [../architecture/repository-and-services.md](../architecture/repository-and-services.md)
- [../architecture/overview.md](../architecture/overview.md)
- [../architecture/api-tools-and-agent.md](../architecture/api-tools-and-agent.md)
