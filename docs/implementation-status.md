# Implementation status

**Last updated:** 2026-07-14

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
| Alembic env + migrations (`tenants`, `jobs`, leases, attempts, dead letters) | yes | yes (up/down/up through `0003`; legacy job + `system.noop` backfill) | full product schema pending |
| ORM models agree with migration (columns/FKs/indexes) | yes | yes | IDs caller-supplied TEXT; no UUIDv7 helper |
| Domain job lifecycle/failure types + ports Protocol (model-free) | yes | yes | immutable failure results; no SQLAlchemy in domain/ports; second-precision times; min 1s lease TTL |
| JobRepository atomic execution lifecycle + leader fencing | yes | yes | claim creates one attempt; complete/fail/retry/dead-letter/expiry history are fenced local short transactions |
| Durable attempt history + one-to-one dead-letter records | yes | yes | retry scheduling uses exact caller-provided delay; redacted failure messages capped at 500 chars |
| Leader lease owner/expiry pair + nonempty name checks | yes | yes | migration `0002` + model agreement |
| FastAPI app factory + `python -m akunaki.api` | yes | yes | |
| `GET /healthz` (service, db ready, `models_required=false`) | yes | yes | does not fabricate product health |
| Worker entry `python -m akunaki.worker` stub | yes | yes | **no job claim loop** |
| Core-only / no model SDKs | yes | yes | import-linter + tests |
| Ruff / mypy / import-linter / pytest / pip-audit gates | yes | yes | see evidence docs |
| Frontend / web | no | no | deferred |
| Auth / OIDC / sessions product | no | no | deferred |
| Connectors (Oura, Google Health, Polar) | no | no | deferred |
| Agent / model packages | no | no | forbidden in core |
| Full data-model schema | no | no | only tenants and durable-job lifecycle tables exist |
| Worker claim / heartbeat / scheduler / reaper runtime | no | no | repository lifecycle exists; worker entry remains a stub |
| Runtime retry classification / backoff policy / handlers | no | no | durable retry scheduling exists; execution policy does not |
| Remote production Turso (Turso Cloud) | no | no | **product deferred**; proposed in ADR 0003 only |
| Encryption-at-rest / backup evidence | no | no | Phase Zero open |
| Volume / vector spikes | no | no | Phase Zero open |

---

## Process entrypoints

| Entrypoint | Behavior today |
|------------|----------------|
| `python -m akunaki.api` | Serves core API; `/healthz` only |
| `python -m akunaki.worker` | Boots config/DB, readiness, stub message, clean exit |
| `python -m akunaki.agent_worker` | **not present** |
| `python -m akunaki.mcp_adapter` | **not present** |

---

## Documentation vs code

| Area | State |
|------|-------|
| `docs/` architecture set | Proposed design (mostly still forward-looking; Turso Cloud remains **future** context) |
| `backend/` | First real application code (this foundation) |
| Phase Zero overall | **In progress** — local libSQL foundation + atomic durable-job repository lifecycle tested; remote Turso intentionally deferred; worker runtime / encryption / volume open |

Evidence: [evidence/phase-zero-turso-foundation.md](evidence/phase-zero-turso-foundation.md), [evidence/phase-zero-job-concurrency.md](evidence/phase-zero-job-concurrency.md).
