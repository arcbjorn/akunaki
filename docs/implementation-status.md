# Implementation status

**Last updated:** 2026-07-18

Honest truth table for what exists in this repository. Prefer this page over roadmap marketing language when asking “is it built?”

Legend:

- **Implemented** — code present in tree
- **Tested** — automated tests green on the documented platform
- **Pending** — designed in docs; not built or not proven

---

## Platform foundation (Phase Zero partial)

| Capability | Implemented | Tested | Pending / notes |
|------------|:-----------:|:------:|-----------------|
| Backend package layout (`src/akunaki/{domain,application,ports,adapters/db,api,worker}`) | yes | yes (import/boot) | layers mostly empty by design |
| Python 3.13.14 pin + `requires-python >=3.13.14,<3.14` | yes | yes | 3.14 rejected after segfault observation |
| Exact dependency pins + `uv.lock` | yes | yes (`uv lock --check`) | stable-only policy |
| pydantic-settings config (`AKUNAKI_` prefix) | yes | yes | no model config surface |
| Safe local default `sqlite+libsql` URL | yes | yes | parent dir created on engine build |
| Local-only `database_url` validation (official `sqlite+libsql://` memory / path memory / relative / absolute file) | yes | yes | rejects hostname, credentials, port, query, fragment, non-dialect |
| Remote Turso auth token / connect_args | **no** | n/a | **intentionally deferred** (not wired; not credential-blocked) |
| SQLAlchemy 2 engine/session + FK pragma + busy_timeout(50ms) + file WAL once | yes | yes | StaticPool in-memory; QueuePool file-backed (pool_size=5, max_overflow=5, pool_timeout=5) |
| Declarative base + naming conventions | yes | yes | |
| Database readiness probe | yes | yes | |
| Alembic env + migrations (`tenants`, `jobs`, leases, attempts, dead letters, connections) | yes | yes (up/down/up through `0004`; legacy job + `system.noop` backfill; head derived, not hardcoded) | full product schema pending |
| Connection lifecycle schema (`connections`, `connection_secrets`, `connection_health`) | yes | yes | migration `0004`; one connection per provider per tenant; provider/status vocabularies; ciphertext-only token storage; cascade delete |
| libSQL-compatible `Blob` column type | yes | yes | driver exposes no DBAPI `Binary`, so stock `LargeBinary` cannot bind — see Turso evidence note 4 |
| ORM models agree with migration (columns/FKs/indexes) | yes | yes | IDs caller-supplied TEXT; no UUIDv7 helper |
| Domain job lifecycle/failure types + ports Protocol (model-free) | yes | yes | immutable failure results; no SQLAlchemy in domain/ports; second-precision times; min 1s lease TTL |
| JobRepository atomic execution lifecycle + leader fencing | yes | yes | claim creates one attempt; complete/fail/retry/dead-letter/expiry history are fenced local short transactions |
| Idempotent enqueue (`enqueue_job`) deduped on `(tenant_id, idempotency_key)` | yes | yes | atomic `INSERT ... ON CONFLICT DO NOTHING`; NULL key always inserts; tenant-scoped; duplicate job id without a key raises |
| Durable attempt history + one-to-one dead-letter records | yes | yes | retry scheduling uses exact caller-provided delay; redacted failure messages capped at 500 chars |
| Leader lease owner/expiry pair + nonempty name checks | yes | yes | migration `0002` + model agreement |
| FastAPI app factory + `python -m akunaki.api` | yes | yes | |
| `GET /healthz` (service, db ready, `models_required=false`) | yes | yes | does not fabricate product health |
| Worker entry `python -m akunaki.worker` runtime | yes | yes | claim loop + SIGINT/SIGTERM cooperative shutdown; JSON logs |
| Worker runtime (claim → execute → heartbeat → settle) | yes | yes | port-typed in `application`; fake-repository policy tests + file-backed end-to-end tests |
| Retry classification + exponential backoff policy | yes | yes | transient/permanent/cancelled; capped jitter; min 1s (second-precision lifecycle) |
| Handler registry (`system.noop` built in) | yes | yes | unregistered `job_type` dead-letters instead of burning attempts |
| Background lease heartbeat | yes | yes | daemon thread; lease loss suppresses completion (no false success) |
| Leader-gated reaper tick (requeue expired / dead-letter exhausted) | yes | yes | standby never reaps without the `core-reaper` leader lease |
| Core-only / no model SDKs | yes | yes | import-linter + tests |
| Ruff / mypy / import-linter / pytest / pip-audit gates | yes | yes | see evidence docs |
| Frontend / web | no | no | deferred |
| Auth / OIDC / sessions product | no | no | deferred |
| Connectors (Oura, Google Health, Polar) | no | no | connection **schema** exists; no OAuth, HTTP client, sync, or normalization code |
| Agent / model packages | no | no | forbidden in core |
| Full data-model schema | no | no | tenants, durable-job lifecycle, and connection lifecycle tables exist; OAuth state, raw/sync transport, facts, and scores pending |
| Envelope encryption (AES-256-GCM, KEK/DEK, rotation, AAD binding) | yes | yes | fresh DEK+nonces per seal; versioned KEK registry; fail-fast boot without keys; mutation-checked randomness |
| Sealed tokens persisted to `connection_secrets` | yes | yes | raw column holds no readable token; envelope bound to its connection; cascade delete |
| KEK sourcing from external KMS / secret manager | no | no | keys load from `AKUNAKI_SECRET_KEKS` only; no KMS client, rotation runbook, or key-use audit |
| OAuth flow (`oauth_states`, token exchange, refresh) | no | no | sealing exists; **no** OAuth state schema, provider client, or token acquisition |
| Concurrent worker runtimes (exactly-once execution, single leader, stolen-lease safety) | yes | yes | bounded local stress: 3 workers/24 jobs, 4 contending reapers, independent engines |
| Sustained multi-process fleet under production load | no | no | in-process threads with independent engines only; no long-running or cross-host soak |
| Product job handlers (connectors, normalization, scoring) | no | no | only `system.noop` exists; registry is ready for them |
| Atomic domain side-effect unit of work | no | no | lease validity primitive exists; fenced side-effect UoW still pending |
| Remote production Turso (Turso Cloud) | no | no | **product deferred**; proposed in ADR 0003 only |
| Encryption-at-rest / backup evidence | partial | partial | application-level envelope for secret columns done (see [evidence](evidence/phase-zero-envelope-encryption.md)); platform at-rest, backup/export encryption, and key separation runbooks open |
| Volume / vector spikes | no | no | Phase Zero open |

---

## Process entrypoints

| Entrypoint | Behavior today |
|------------|----------------|
| `python -m akunaki.api` | Serves core API; `/healthz` only |
| `python -m akunaki.worker` | Boots config/DB, readiness, then runs the durable claim loop until SIGINT/SIGTERM |
| `python -m akunaki.agent_worker` | **not present** |
| `python -m akunaki.mcp_adapter` | **not present** |

---

## Documentation vs code

| Area | State |
|------|-------|
| `docs/` architecture set | Proposed design (mostly still forward-looking; Turso Cloud remains **future** context) |
| `backend/` | First real application code (this foundation) |
| Phase Zero overall | **In progress** — local libSQL foundation, atomic durable-job repository lifecycle, and single-process worker runtime tested; remote Turso intentionally deferred; encryption / volume / connector spikes open |

Evidence: [evidence/phase-zero-turso-foundation.md](evidence/phase-zero-turso-foundation.md), [evidence/phase-zero-job-concurrency.md](evidence/phase-zero-job-concurrency.md).
