# Implementation status

**Last updated:** 2026-07-13

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
| SQLAlchemy 2 engine/session + FK pragma | yes | yes | official local `sqlite+libsql` dialect |
| Declarative base + naming conventions | yes | yes | |
| Database readiness probe | yes | yes | |
| Alembic env + initial migration (`tenants`, `jobs`) | yes | yes (up/down/up) | full product schema pending |
| ORM models agree with migration (columns/FKs/indexes) | yes | yes | IDs caller-supplied TEXT; no UUIDv7 helper |
| FastAPI app factory + `python -m akunaki.api` | yes | yes | |
| `GET /healthz` (service, db ready, `models_required=false`) | yes | yes | does not fabricate product health |
| Worker entry `python -m akunaki.worker` stub | yes | yes | **no job claim loop** |
| Core-only / no model SDKs | yes | yes | import-linter + tests |
| Ruff / mypy / import-linter / pytest / pip-audit gates | yes | yes | see evidence doc |
| Frontend / web | no | no | deferred |
| Auth / OIDC / sessions product | no | no | deferred |
| Connectors (Oura, Google Health, Polar) | no | no | deferred |
| Agent / model packages | no | no | forbidden in core |
| Full data-model schema | no | no | only tenants + jobs foundation |
| Job concurrency protocol (CAS claim, leases, fence) | no | no | table shape only |
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
| Phase Zero overall | **In progress** — local libSQL / Turso-compatible storage is the implemented scope; remote Turso intentionally deferred; concurrency + other spikes open |

Evidence: [evidence/phase-zero-turso-foundation.md](evidence/phase-zero-turso-foundation.md).
