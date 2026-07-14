# Architecture overview

**Status:** Proposed

**Last reviewed:** 2026-07-13

This page describes the proposed end-to-end shape of Akunaki. No runtime components exist in this repository yet.

---

## Product shape

Akunaki targets a single-user-centric health intelligence experience:

1. User authenticates (OIDC authorization code + PKCE).
2. User connects Oura, Google Health, and/or Polar via least-privilege OAuth (Google Health uses Google OAuth and restricted scopes).
3. Core worker syncs exact vendor payloads into durable pages and immutable raw revisions, normalizes typed facts, applies source policy selections, and recomputes affected local health days.
4. Deterministic engine produces features, baselines, scores (by `score_code`), anomalies, and rule recommendations via reproducible derivation runs.
5. Web PWA answers **How am I / Why / What should I do** from product API responses under `/v1`.
6. Optional model agent explains structured summaries via tools; product remains complete with models and agent-worker disabled or absent.

---

## Logical system diagram

```text
                    +------------------+
                    |  Next.js PWA     |
                    |  (web/)          |
                    +--------+---------+
                             | HTTPS REST + SSE (/v1)
                             v
                    +------------------+
                    |  FastAPI API     |
                    |  (backend API    |
                    |   process)       |
                    +--------+---------+
                             |
              shared Python package (backend/)
                             |
         +-------------------+-------------------+
         |                   |                   |
         v                   v                   v
  application        domain (pure)         infrastructure
  services           health engine         SQLAlchemy/libSQL
  tool registry      source policy         job queue tables
  OAuth/session      normalizers           connector HTTP
         |                   |                   |
         +-------------------+-------------------+
                             |
                             v
                    +------------------+
                    |  Turso (prod)    |
                    |  libSQL/SQLite   |
                    |  (dev/CI)        |
                    +--------+---------+
                             ^
              +--------------+--------------+
              |                             |
     +--------+---------+         +---------+----------+
     |  Core worker/    |         |  Agent-worker      |
     |  scheduler       |         |  (optional)        |
     |  (required)      |         |  same package      |
     +--------+---------+         +---------+----------+
              |                             |
   connectors: oura |              model adapters
   google_health | polar           (optional extra)
              |
              v
     vendor APIs + provider-specific webhooks

     [optional MCP adapter process → tool registry]
```

---

## Process topology (MVP + optional phase four)

| Process | Role | Sharing |
|---------|------|---------|
| **API** | Auth, product REST `/v1`, SSE, webhook ingress (verify + durable inbox), enqueue agent runs | Shares `backend/` package and DB; **no** model SDK at core boot |
| **Core worker/scheduler** | Claims core jobs: sync, normalize, recompute, export, delete; scheduled reconciliation | Same package and DB; one active core worker initially |
| **Agent-worker** | Optional; claims `agent.run` only; model I/O + tools | Same package; **optional dependency extra**; failure isolated |
| **MCP adapter** | Optional; stdio / Streamable HTTP over tool registry | Same package; not a second business layer |
| **Web** | Next.js TypeScript PWA; BFF only where CSRF/session cookies require it | Talks only to product API; no domain formulas |

All backend processes share **one operational database**: **Turso selected for production**; local libSQL/SQLite in development and many CI relational tests. Physical design: SQLite/libSQL conventions in [data-model.md](data-model.md). Phase zero validates the exact Python/SQLAlchemy/Alembic, concurrency, migration, encryption, volume, and later vector path; only a **proven blocker** reopens [ADR 0003](../adr/0003-libsql-operational-store.md). No DuckDB in MVP. External broker evaluation begins when **any** sustained scale trigger fires—see [repository-and-services.md](repository-and-services.md).

---

## Data flow (happy path)

1. **Connect:** User completes provider OAuth; access/refresh tokens are envelope-encrypted in `connection_secrets` (not stored in plaintext on `connections`). Connection metadata and health live on `connections` / `connection_health`.
2. **Schedule / webhook:** Scheduler enqueues sync jobs. Webhooks are **provider-specific**: Google Health verifies rotating public-key signatures plus endpoint authorization; Oura and Polar use their documented signatures. API verifies, persists a durable deduplicated `webhook_inbox` row, acknowledges quickly, then enqueues refetch. Scheduled reconciliation covers gaps.
3. **Fetch:** Connector returns `RawEnvelope`(s); `raw_payload` pages retain exact bodies and redacted metadata; `raw_objects` / append-only `raw_revisions` link to payload and `sync_runs`. Cursor, raw records, and normalization outbox commit atomically after fetch (crash-safe replay).
4. **Normalize:** Normalizer emits typed `fact_records` headers plus one-to-one detail tables with UTC instant, source offset, IANA timezone, local health day, units, quality, confidence, device, origin, method, lineage.
5. **Select:** match session/workout facts into stable provider-independent `source_grains` with versioned membership in `source_grain_versions` / `source_grain_members` (`match_algorithm_version` pinned per version); one current `source_selections` decision per non-null `grain_key` with `source_grain_version_id` required for session/workout (pins membership snapshot; selected fact must be a member); nullable real `selected_fact_record_id` (null only for `missing_authoritative` + `missing_reason`); alternatives in `source_selection_candidates` only (members of pinned version unless ineligible near-match with reason); no averaging; no silent fallback; candidate `rank` is display order only; overlapping Google/Fitbit-origin workout samples excluded from internal load when Polar covers the interval.
6. **Recompute:** For affected local health days, pure stages recorded as `derivation_runs` / typed `derivation_inputs`: `daily_health_features` → baselines → `daily_health_scores`/factors → anomalies → recommendations (`general_recovery_v0.1.0` executable and unvalidated).
7. **Serve:** Product API returns today/history with status, scores, confidence, factors, recommendations, opaque provenance URL, freshness, formula/policy versions, and coverage disclosure—without table/raw ids or ETags in response bodies.
8. **Optional agent:** API persists conversation events and queues `agent.run`. Agent-worker (if deployed) loads structured summaries and tools; scores cannot be invented. If agent is intentionally disabled → `409 agent_disabled`; agent outage → `503`; core product continues.

---

## Domain modules (proposed package map)

| Module | Responsibility |
|--------|----------------|
| `domain.health` | Pure scoring, baselines, anomalies, recommendations, load math |
| `domain.policy` | Source policy evaluation (pure given policy rows + candidates) |
| `domain.normalize.*` | Provider normalizers (pure given RawEnvelope content) |
| `application.*` | Use cases: connect, sync orchestration, recompute, export, delete, tools |
| `ports.connectors` | Connector protocol, OAuth, webhooks, cursors |
| `ports.models` | Async model provider interface (bound only when agent extra present) |
| `ports.jobs` | Job enqueue/claim/complete abstractions |
| `adapters.db` | SQLAlchemy models, repositories, Alembic |
| `adapters.connectors.*` | HTTP/OAuth implementations (`oura`, `google_health`, `polar`) |
| `adapters.models.*` | Provider-specific LLM adapters (optional extra) |
| `api` | FastAPI routes, auth middleware, SSE, webhook ingress |
| `worker` | Core scheduler ticks, claim loop, handlers, reconciliation |
| `agent_worker` | Optional agent job claim loop |
| `mcp_adapter` | Optional MCP transport |

Dependency rule: `domain` imports nothing from adapters or API. `application` depends on domain and ports. Adapters implement ports. API and workers call application only. See [repository-and-services.md](repository-and-services.md).

---

## Trust and safety boundaries

| Boundary | Rule |
|----------|------|
| Browser | No persistent health JSON in `localStorage`/`IndexedDB` by default; authenticated `/v1` is NetworkOnly + `private, no-store` |
| API | Tenant authorization on every resource (composite tenant auth; no RLS); session cookies + CSRF for cookie auth |
| Core worker | Fence tokens on job leases; idempotent handlers; atomic cursor/raw/outbox commit |
| Models / agent | Optional; minimum structured summaries; confirmation for mutations; isolated process |
| Logs | PHI-free; pseudonymized tenant labels; redacted secrets |
| Deletion | Privacy hard-scrub overrides immutability; drains jobs; hard-deletes health/conversation/egress/vector/export data; **minimal completion proof** plus **access-separated restoration-suppression ledger** (HMAC selectors; retain until backups expire + 30 days) replayed before restored data is served |

Details: [security.md](security.md).

---

## What is deliberately deferred

| Deferred | Until |
|----------|-------|
| **Apple Health / HealthKit** | Future **native iOS bridge** only: device-local, fine-grained user-authorized; syncs typed, provenance-preserving records to the backend. **Not** a server connector. **No** native mobile app in MVP. Distinct from Android Health Connect. |
| Android Health Connect companion | Phase five+ companion bridge (on-device; not a server connector) |
| Nutrition logging depth | Phase five (schema stubs may land earlier) |
| Optional vector retrieval / embeddings | Phase four / future agent; deterministic SQL remains source of truth without embeddings |
| MCP remote adapter | Phase four |
| Optional agent-worker + model extras | Phase four |
| Redis / multi-worker broker | Any sustained scale trigger (see repository-and-services) |
| DuckDB / warehouse | Post-MVP analytics need |
| Multi-region active-active | Measured geo need |
| Legacy Fitbit Web API | Never for MVP (September 2026 sync stop) |
| Google Fit as foundation | Never |

Full sequence: [../roadmap.md](../roadmap.md). Connector matrix: [ingestion-and-sync.md](ingestion-and-sync.md).

---

## Related pages

- [repository-and-services.md](repository-and-services.md) — layout, services, jobs
- [data-model.md](data-model.md) — schema
- [ingestion-and-sync.md](ingestion-and-sync.md) — connectors and sync
- [health-engine.md](health-engine.md) — deterministic engine
- [api-tools-and-agent.md](api-tools-and-agent.md) — API, tools, models, MCP
- [frontend.md](frontend.md) — PWA
- [../adr/0001-modular-monolith.md](../adr/0001-modular-monolith.md)
- [../adr/0003-libsql-operational-store.md](../adr/0003-libsql-operational-store.md)
