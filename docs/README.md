# Akunaki documentation

**Status:** Proposed architecture + **partial Phase Zero implementation**

**Last reviewed:** 2026-07-13

This repository is **primarily documentation**, with a **first backend foundation** under `backend/` (model-free FastAPI + local libSQL / Turso-compatible storage + Alembic). **Turso Cloud / remote** is intentionally deferred by product decision (not wired). Most product surfaces (connectors, auth, scoring, frontend, agent) are **not** implemented. See [implementation-status.md](implementation-status.md) for an honest implemented / tested / pending table.

Nothing outside the documented foundation should be assumed to exist at runtime.

---

## Navigation

| Area | Document |
|------|----------|
| **Implementation status** | [implementation-status.md](implementation-status.md) |
| **Phase Zero evidence (local libSQL foundation)** | [evidence/phase-zero-turso-foundation.md](evidence/phase-zero-turso-foundation.md) |
| Product principles | [product-principles.md](product-principles.md) |
| Glossary | [glossary.md](glossary.md) |
| Architecture overview | [architecture/overview.md](architecture/overview.md) |
| Repository and services | [architecture/repository-and-services.md](architecture/repository-and-services.md) |
| Data model | [architecture/data-model.md](architecture/data-model.md) |
| Ingestion and sync | [architecture/ingestion-and-sync.md](architecture/ingestion-and-sync.md) |
| Health engine | [architecture/health-engine.md](architecture/health-engine.md) |
| API, tools, and agent | [architecture/api-tools-and-agent.md](architecture/api-tools-and-agent.md) |
| Frontend | [architecture/frontend.md](architecture/frontend.md) |
| Security and privacy | [architecture/security.md](architecture/security.md) |
| Operations | [architecture/operations.md](architecture/operations.md) |
| Testing | [testing.md](testing.md) |
| Roadmap | [roadmap.md](roadmap.md) |
| References | [references.md](references.md) |
| Architecture decision records | [adr/README.md](adr/README.md) |

### Backend code

| Path | Notes |
|------|-------|
| [`backend/`](../backend/) | Core package, Alembic, tests |
| [`backend/README.md`](../backend/README.md) | Setup, test, run, migrate, audit commands |

### ADR index

| ADR | Title |
|-----|-------|
| [0001](adr/0001-modular-monolith.md) | Modular monolith |
| [0002](adr/0002-deterministic-core.md) | Deterministic core |
| [0003](adr/0003-libsql-operational-store.md) | libSQL operational store |
| [0004](adr/0004-versioned-provenance.md) | Versioned provenance |
| [0005](adr/0005-authoritative-source-policy.md) | Authoritative source policy |

---

## Coverage matrix

Every decision the architecture must settle maps to an authoritative page. Secondary pages may elaborate but do not contradict the authoritative source.

| # | Decision | Authoritative page |
|---|----------|--------------------|
| 1 | Service boundaries | [architecture/repository-and-services.md](architecture/repository-and-services.md) |
| 2 | Repository structure | [architecture/repository-and-services.md](architecture/repository-and-services.md) |
| 3 | Database schema | [architecture/data-model.md](architecture/data-model.md) |
| 4 | Connector interface | [architecture/ingestion-and-sync.md](architecture/ingestion-and-sync.md) |
| 5 | Raw and normalized models | [architecture/data-model.md](architecture/data-model.md), [architecture/ingestion-and-sync.md](architecture/ingestion-and-sync.md) |
| 6 | Sync | [architecture/ingestion-and-sync.md](architecture/ingestion-and-sync.md) |
| 7 | Source priority | [architecture/ingestion-and-sync.md](architecture/ingestion-and-sync.md), [adr/0005-authoritative-source-policy.md](adr/0005-authoritative-source-policy.md) |
| 8 | Deterministic scoring | [architecture/health-engine.md](architecture/health-engine.md), [adr/0002-deterministic-core.md](adr/0002-deterministic-core.md) |
| 9 | Model provider | [architecture/api-tools-and-agent.md](architecture/api-tools-and-agent.md) |
| 10 | Tool registry | [architecture/api-tools-and-agent.md](architecture/api-tools-and-agent.md) |
| 11 | MCP | [architecture/api-tools-and-agent.md](architecture/api-tools-and-agent.md) |
| 12 | Product API | [architecture/api-tools-and-agent.md](architecture/api-tools-and-agent.md) |
| 13 | Jobs | [architecture/repository-and-services.md](architecture/repository-and-services.md), [architecture/operations.md](architecture/operations.md) |
| 14 | Security | [architecture/security.md](architecture/security.md) |
| 15 | Deployment | [architecture/operations.md](architecture/operations.md) |
| 16 | Tests | [testing.md](testing.md) |
| 17 | Observability | [architecture/operations.md](architecture/operations.md) |
| 18 | Migrations | [architecture/data-model.md](architecture/data-model.md), [architecture/operations.md](architecture/operations.md) |
| 19 | Frontend | [architecture/frontend.md](architecture/frontend.md) |
| 20 | Phased plan | [roadmap.md](roadmap.md) |

---

## How to read this set

1. Start with [product-principles.md](product-principles.md) and [glossary.md](glossary.md).
2. Read [architecture/overview.md](architecture/overview.md) for the end-to-end shape.
3. Check [implementation-status.md](implementation-status.md) before assuming code exists.
4. Drill into domain docs as needed; use ADRs for decision history and reversal conditions.
5. Treat [roadmap.md](roadmap.md) as the implementation sequence; Phase Zero is **in progress**, not complete.

---

## Document conventions

- Architecture pages remain **Status: Proposed** unless they describe shipped code.
- Language distinguishes *proposed* design from *implemented* foundation.
- Cross-links are relative within `docs/`.
- External links appear in [references.md](references.md) and evidence pages; prefer primary official sources.
- Open validation items are called out explicitly rather than papered over.
