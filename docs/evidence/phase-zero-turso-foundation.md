# Phase Zero evidence: Turso / libSQL + SQLAlchemy + Alembic foundation

**Date:** 2026-07-13

**Status:** Partial — **local** libSQL / Turso-compatible path validated; **Turso Cloud / remote intentionally deferred** (product decision; not wired; not blocked on credentials)

**Authoritative context:** [ADR 0003](../adr/0003-libsql-operational-store.md), [roadmap Phase Zero](../roadmap.md)

---

## Implemented scope (product decision)

| Scope | Status |
|-------|--------|
| Local `sqlite+libsql` (official empty in-memory, path in-memory, relative file, absolute file) | **Implemented and tested** |
| Settings validation rejecting host / credentials / port / query / fragment / non-dialect URLs | **Implemented and tested** |
| Parent directory creation for local file URLs | **Implemented** (on engine build) |
| Remote Turso Cloud credentials, auth token settings, connect_args wiring | **Not present** — intentionally deferred |
| Long-term production Turso architecture | **Proposed** in ADRs / architecture docs only (future context) |

This foundation is **local database only**. Remote production connectivity is **not** an open credential gap; it is out of current implementation scope by product decision.

---

## Tested platform

| Field | Value |
|-------|-------|
| OS | macOS (Darwin), aarch64 (Apple Silicon) |
| Shell | zsh |
| Package manager | uv 0.11.24 |
| Working directory | `backend/` |

---

## Exact versions under test

| Component | Version |
|-----------|---------|
| Python | **3.13.14** (pinned in `backend/.python-version`) |
| `requires-python` | `>=3.13.14,<3.14` |
| fastapi | 0.139.0 |
| uvicorn | 0.51.0 |
| pydantic | 2.13.4 |
| pydantic-settings | 2.14.2 |
| SQLAlchemy | 2.0.51 |
| alembic | 1.18.5 |
| sqlalchemy-libsql | 0.2.0 |
| libsql-experimental (transitive) | 0.0.55 |
| httpx2 | 2.5.0 |
| pytest | 9.1.1 |
| pytest-cov | 7.1.0 |
| ruff | 0.15.21 |
| mypy | 2.3.0 |
| import-linter | 2.13 |
| pip-audit | 2.10.1 |
| uv_build | 0.11.28 |

Lockfile: `backend/uv.lock` (generated with `uv lock`).

---

## Official source links

| Topic | URL |
|-------|-----|
| Turso + SQLAlchemy (official) | https://docs.turso.tech/sdk/python/orm/sqlalchemy |
| sqlalchemy-libsql (GitHub) | https://github.com/tursodatabase/sqlalchemy-libsql |
| Turso Python SDK quickstart | https://docs.turso.tech/sdk/python/quickstart |
| libSQL overview | https://docs.turso.tech/libsql |
| SQLAlchemy 2 engine | https://docs.sqlalchemy.org/en/20/core/engines.html |
| Alembic tutorial | https://alembic.sqlalchemy.org/en/latest/tutorial.html |
| FastAPI | https://fastapi.tiangolo.com/ |
| pydantic-settings | https://docs.pydantic.dev/latest/concepts/pydantic_settings/ |
| httpx2 (TestClient client) | https://pypi.org/project/httpx2/ |
| Starlette testing / TestClient | https://www.starlette.io/testclient/ |

---

## Python compatibility gate (honest)

| Runtime | Driver | Observation |
|---------|--------|-------------|
| **Python 3.14.5** + `sqlalchemy-libsql==0.2.0` | macOS ARM | **Segfault** observed (process abort; not a clean Python exception). |
| **Python 3.13.x** + `sqlalchemy-libsql==0.2.0` | macOS ARM | **Works** for local `sqlite+libsql` file URL, migrations, CRUD, and readiness probe. |

**Decision:** Pin the foundation to **Python 3.13.14** and `requires-python >=3.13.14,<3.14`. Do **not** claim 3.14 support until the driver/runtime stack is re-proven on this platform.

This is a **compatibility gate**, not a product rejection of the long-term Turso store choice in ADR 0003. ADR 0003 remains the **proposed** production architecture; current code implements local libSQL only.

---

## Test matrix and results

All commands run from `backend/` after `uv sync --all-groups` on Python 3.13.14.

| Check | Command | Result |
|-------|---------|--------|
| Lock consistency | `uv lock --check` | PASS (Resolved 67 packages) |
| Lint | `uv run ruff check` | PASS |
| Format | `uv run ruff format --check` | PASS (27 files already formatted) |
| Types | `uv run mypy src tests` | PASS (Success: no issues found in 25 source files) |
| Import boundaries | `uv run lint-imports` | PASS (Kept 5 contracts) |
| Unit/integration | `uv run pytest` | PASS (65 passed; `filterwarnings=error`) |
| Audit | `uv run pip-audit` | PASS (No known vulnerabilities found; local `akunaki` skipped — not on PyPI) |
| Freshness | `uv tree --outdated` | Direct pins current with the documented pydantic-core upstream constraint |
| Package build | `uv build` | PASS (source distribution and wheel) |

### pytest coverage (what was proven)

| Area | Result |
|------|--------|
| API core-only boot (no model config) | PASS |
| `GET /healthz` typed response (`models_required=false`) | PASS |
| Settings `AKUNAKI_` prefix + **local-only** dialect validation | PASS |
| Accepted local URL forms (official empty memory / path memory / relative / absolute) | PASS |
| Rejected remote host / credentials / port / query / fragment / non-dialect URLs | PASS |
| No `database_auth_token` / remote connect_args path | PASS (absent by design) |
| FK enforcement + basic tenant/job CRUD on temp libSQL file | PASS |
| `json_valid` check + idempotency uniqueness | PASS |
| Alembic upgrade → downgrade → upgrade on temp file | PASS |
| Migration vs ORM column/index/FK agreement | PASS |
| Model SDK absence (installed dists, imports, pyproject deps) | PASS |
| Worker stub boots config/DB and exits without claim loop | PASS |
| No leftover DB artifacts outside pytest `tmp_path` | PASS (by fixture design) |

### Local libSQL result

**Validated:** local URL forms using the official dialect:

- official in-memory `sqlite+libsql://` (sqlalchemy-libsql documented form)
- path in-memory `sqlite+libsql:///:memory:`
- relative file (e.g. `sqlite+libsql:///.local/akunaki.db`)
- absolute file (e.g. `sqlite+libsql:////abs/path/to/file.db`)

With:

- engine creation (no auth token / connect_args)
- parent directory creation for file-backed URLs
- `PRAGMA foreign_keys=ON` on connect
- readiness `SELECT 1`
- Alembic migration chain for minimal `tenants` + `jobs`
- ORM insert/select and constraint failures
- settings rejection of remote hosts, credentials, ports, query strings, fragments, and non-`sqlite+libsql` dialects

### Production remote Turso connection

**Intentionally deferred** by product decision. Current code does **not** contain:

- `database_auth_token` / `AKUNAKI_DATABASE_AUTH_TOKEN`
- remote connect_args / auth-token wiring
- remote URL helpers

This is **not** blocked on missing credentials. Long-term Turso Cloud production store remains **proposed** architecture (ADR 0003 and related docs), not current implementation scope.

---

## Notes reported without suppression

1. **Starlette / TestClient:** Dev dependency is **`httpx2==2.5.0`**. Starlette 1.3.1 prefers httpx2 and deprecates plain httpx for `TestClient`. With this pin there is **no** `StarletteDeprecationWarning` from the TestClient path; pytest runs with `filterwarnings=error` so any recurrence would fail the suite.
2. **Transitive freshness / pydantic-core:** **pydantic 2.13.4** is the latest stable top-level Pydantic release as of **2026-07-13**. **pydantic-core** is a separate internal package with an independent version sequence; Pydantic 2.13.4 requires **pydantic-core 2.46.4** exactly. Therefore **2.13 versus 2.46 is not an age comparison**, and **core 2.47.0 must not be forced**. Do not change the Pydantic pin. `uv tree --outdated` may still report `pydantic-core v2.46.4 (latest: v2.47.0)` under that constraint — treat it as expected, not a pin error.
3. **sqlalchemy-libsql constraint errors:** some SQLite constraint failures surface as `ValueError` (not always SQLAlchemy `IntegrityError`). Tests accept both; application code should treat this as a known driver quirk.
4. **`libsql_experimental` has no DBAPI `Binary` constructor (BLOB bind gap).** The driver **stores and returns BLOBs correctly** — a full 256-byte round-trip is byte-exact — but it does not expose the optional DBAPI `Binary` attribute that SQLAlchemy's stock `LargeBinary.bind_processor` looks up. Binding through `LargeBinary` therefore raises `AttributeError: module 'libsql_experimental' has no attribute 'Binary'` **before** the statement executes; `sqlalchemy-libsql==0.2.0` ships no shim. Worked around by `akunaki.adapters.db.types.Blob`, a `TypeDecorator` whose `bind_processor` returns `None` so `bytes` pass straight through. Emitted DDL is still `BLOB`, so this is a **driver-binding** workaround, not a schema relaxation. Two tests pin the quirk (`test_libsql_driver_still_lacks_dbapi_binary`, `test_stock_large_binary_still_fails_on_this_driver`) and fail deliberately if a future driver release adds `Binary`, at which point `Blob` can be dropped. **Relevant to the encryption spike:** envelope-encrypted ciphertext columns depend on this path.
5. **Full schema / concurrency protocol:** job claim CAS, leases, and multi-worker races on **local** libSQL are covered in [phase-zero-job-concurrency.md](phase-zero-job-concurrency.md). Full product schema and expand/contract rolling migrations remain **pending**; the worker claim loop is implemented and covered there.

---

## What this evidence does *not* claim

- Remote Turso Cloud connectivity (deferred; not wired)
- Job claim loop / fencing / multi-worker stress harness (covered separately in [phase-zero-job-concurrency.md](phase-zero-job-concurrency.md), not by this document)
- Encryption-at-rest / backup policy validation
- Volume / minute-level HR cardinality
- Vector / ANN path
- Connector or auth product surfaces
- Any model-provider integration

---

## Related

- [implementation-status.md](../implementation-status.md)
- [backend/README.md](../../backend/README.md)
- [architecture/repository-and-services.md](../architecture/repository-and-services.md)
- [adr/0003-libsql-operational-store.md](../adr/0003-libsql-operational-store.md)
