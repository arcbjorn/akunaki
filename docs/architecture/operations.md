# Operations

**Status:** Proposed

**Last reviewed:** 2026-07-13

Authoritative for **deployment**, **observability**, and co-authoritative for **jobs** and **migrations** (coverage matrix items 13, 15, 17, 18).

No production environment is defined by running code in this repository.

---

## Deployment topology (MVP target)

| Component | Deploy form | Scaling |
|-----------|-------------|---------|
| `web` | Container or platform Next.js host | Horizontal behind CDN (static/shell only) |
| `api` | Container running FastAPI | Horizontal; sticky sessions not required if session in DB |
| `worker` | Container running **core** worker module | **One active replica** initially; optional **passive standby** |
| `agent-worker` | Optional container; same package + `[agent]` extra | Scale independently for agent jobs only; **not** required for core SLOs |
| `mcp-adapter` | Optional process | Local/staging first |
| `db` | **Turso** (prod), local libSQL/SQLite (dev) | Provider-managed |
| `export-objects` | Private **encrypted** object storage | Time-limited signed access |

```text
[ CDN (shell only) ] → [ web ]
[ Users/OIDC/Webhooks ] → [ api ] → [ Turso ]
                              ↑
[ core worker (1 + optional standby) ] ──┘
[ agent-worker (optional) ] ─────────────┘
[ private encrypted export object store ] ← core worker / api
```

Infra manifests would live under proposed `infra/` (not present yet).

### Core vs agent isolation

- Core install/image can omit model SDKs.
- API and core worker start without model config.
- Agent-worker failure or absence: agent routes return **503** (or **409** if intentionally disabled); ingestion, engine, recommendations, notifications, dashboard, and export continue.
- Model failure is **isolated** and never pages as a core data-plane outage by default.

---

## Configuration management

- 12-factor style env vars ([repository-and-services.md](repository-and-services.md))
- Secrets from platform secret store; never committed
- Config validation at process boot (fail fast)
- Separate env: `local`, `ci`, `staging`, `prod`
- Core processes **must not** require `MODEL_*` variables

---

## Database operations

### Selected store

**Turso** is the selected production operational store. Phase zero validates the exact Python/SQLAlchemy/Alembic, concurrency, migration, encryption, volume, and later vector path; only a **proven blocker** reopens [ADR 0003](../adr/0003-libsql-operational-store.md). Local relational tests may use SQLite; vector integration tests use libSQL/Turso.

### Backup and restore

| Topic | Proposal |
|-------|----------|
| Prod backups | **Encrypted** Turso/provider automated backups + periodic logical export job |
| Local / operator backup | **Safe SQLite backup API** (or online backup protocol)—**not** live file copy of a busy DB |
| Export artifacts | Private encrypted object storage; expiry |
| Retention | Align with privacy policy; deletion requests schedule backup expiry |
| Restore drill | Documented runbook: restore → load **restoration-suppression ledger** → suppress HMAC-matched tenants/objects → verify → serve (completion proofs alone are not sufficient) |
| Restore keys | Separated from backup ciphertext ([security.md](security.md)) |
| Deletion key | Dedicated key for restoration-suppression HMACs; access-separated; destroy ledger entries after backups expire **+ 30 days** |

### Schema migrations

1. Alembic revision in `backend/alembic` (proposed path).
2. CI runs migrations against ephemeral DB.
3. Deploy order: migrate then roll api/core worker (**N / N−1 rolling** expand/contract for breaking changes).
4. Phase zero must prove Turso + SQLAlchemy 2 + Alembic lock/concurrency behavior.

### Formula migrations

- Not DDL-bound: publish new `formula_version`.
- Enqueue recompute ranges.
- Old score rows remain with old version for provenance.

### Optional vectors (later)

- Not MVP DDL.
- If enabled: rebuild indexes/rows on embedding-version change; delete with source; tenant filter with retrieval.

---

## Job operations

| Concern | Practice |
|---------|----------|
| Poison messages | Dead letter after max attempts; alert on rate |
| Lease reaping | Core worker tick expires core leases; agent-worker heartbeats agent leases |
| Backpressure | Per-provider concurrency caps |
| Core worker health | Liveness checks; **restart** unhealthy active worker; optional **passive standby** promotion only after **leader lease/fence** CAS (standby must not schedule or reap without leadership; see [repository-and-services.md](repository-and-services.md)) |
| Scale-out / external broker | Evaluate when **any** sustained SLO/resource/contention trigger holds ([repository-and-services.md](repository-and-services.md))—not only when all triggers fire together |
| Privacy delete | Cancel tenant jobs first (core + agent) |
| Agent isolation | Agent queue depth/failures do not block core job classes |

---

## Observability (PHI-free)

### Logs

- Structured JSON: timestamp, level, service, request_id, **pseudonymized tenant label**, route, status, duration_ms, error_class
- Redact tokens, payloads, measurement values; avoid raw tenant UUIDs as free-text labels

### Clocks and freshness pipeline

Emit and chart (where applicable) distinct timestamps:

| Clock | Meaning |
|-------|---------|
| `source_observed_at` | Vendor-observed time |
| `received_at` | Ingest receive time |
| `normalized_at` | Normalization completion |
| `score_materialized_at` | Derivation/score write time |
| `served_at` | API serve time / freshness presented to user |

### Traces

- OpenTelemetry-compatible spans for API requests and job handlers
- Span attributes exclude health values

### Metrics (candidates)

| Metric | Type |
|--------|------|
| `http_request_duration_ms` | histogram |
| `jobs_queue_wait_ms` | histogram |
| `jobs_in_flight` | gauge |
| `jobs_dead_letters_total` | counter |
| `connector_errors_total{provider,class}` | counter |
| `webhook_verify_total{provider,result}` | counter |
| `webhook_dedupe_total{provider}` | counter |
| `quarantine_revisions_total` | counter |
| `scheduler_heartbeat` | gauge/timestamp |
| `worker_liveness` | gauge |
| `db_contention_total` / lock wait | counter/histogram |
| `freshness_lag_seconds` | histogram |
| `recompute_duration_ms` | histogram |
| `model_calls_total{provider,result}` | counter (agent only) |
| `agent_queue_wait_ms` | histogram (isolated) |

### SLO candidates (initial, unvalidated targets)

| SLO | Candidate target |
|-----|------------------|
| API availability (core routes) | 99.5% monthly (MVP) |
| `GET /v1/today` latency p95 | &lt; 300 ms excluding cold start |
| Sync job success rate | &gt; 99% excluding vendor outages |
| Queue wait p95 user-visible (core) | &lt; 5 min (scale evaluation bound) |
| Agent availability | Separate; must not gate core SLOs |

---

## Runbooks (proposed titles)

1. **Vendor API outage** — mark connection health, pause retries, status page note
2. **Needs reauth spike** — user messaging, OAuth debug without logging tokens
3. **Dead letter drain** — classify, fix, re-enqueue
4. **Failed migration** — rollback plan, expand/contract, N/N−1
5. **Privacy delete stuck** — inspect pipeline state, manual scrub checklist, ledger integrity
6. **Turso connectivity** — failover guidance; core vs agent impact
7. **Core worker lease storm / liveness** — ensure single active worker; check reaper; promote standby
8. **Model / agent-worker failure** — isolate; disable or scale agent; **product continues**
9. **Export object store / restore keys** — rotation, expiry, restore + restoration-suppression replay before serve
10. **Webhook verify failures** — provider key rotation, dedupe inspection
11. **Restoration-suppression ledger lifecycle** — backup expiry + 30-day margin, ledger destroy, deletion-key retirement

Runbook bodies are operational docs to be written during implementation; only titles are fixed here for planning.

---

## Environments

| Env | DB | Models / agent-worker | Connectors |
|-----|----|------------------------|------------|
| local | SQLite/libSQL file | off by default; optional extra | sandbox/mock |
| ci | ephemeral SQLite/libSQL; Turso path for vector suite | **forced off** for core; optional isolated agent job | fixtures |
| staging | Turso staging | optional | vendor sandbox if available |
| prod | **Turso** | optional per tenant consent + deploy | production apps |

---

## Related

- [repository-and-services.md](repository-and-services.md)
- [security.md](security.md)
- [../testing.md](../testing.md)
- [../roadmap.md](../roadmap.md)
- [../adr/0003-libsql-operational-store.md](../adr/0003-libsql-operational-store.md)
