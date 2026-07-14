# ADR 0003: libSQL / Turso operational store

**Status:** Proposed

**Last reviewed:** 2026-07-13

## Context

Akunaki needs a single operational source of truth for tenants, connections, raw revisions, facts, scores, jobs, and (later) optional agent artifacts. The team wants low ops burden, strong local dev parity, and SQL with SQLAlchemy 2 + Alembic.

**Turso** (libSQL) is the **selected production operational store**. Local development and many relational tests use **SQLite** or **libSQL** for parity. DuckDB is excellent analytically but is **not** the MVP operational OLTP store.

Phase zero does **not** re-litigate the product choice of Turso. It validates the **exact** implementation path: Python driver + SQLAlchemy 2 + Alembic, concurrency under API+worker, migrations, encryption-at-rest assumptions, volume/cardinality, and (for later-ready agent features) vector storage options. Only a **proven blocker** reopens this ADR.

## Decision

1. **Production operational store:** **Turso** (selected).
2. **Development / CI relational:** local **SQLite** and/or **libSQL** file or server; same SQLAlchemy models and Alembic chain.
3. **ORM / migrations:** SQLAlchemy 2 + Alembic on every environment.
4. **Job queue:** durable tables in the same operational store (database-leased queue).
5. **No DuckDB in MVP** as operational or required analytics store.
6. **Phase-zero validation (mandatory)** of the exact path—not a generic go/no-go on whether to use Turso:
   - Python + SQLAlchemy 2 driver selection and connection modes
   - Concurrency: API + worker job leases, fencing, no silent corruption
   - Alembic expand/contract / N and N−1 rolling migrations
   - Encryption-at-rest options for DB, backups, and sensitive columns
   - Volume estimates for minute-level timeseries (downsample/cold policy if needed)
   - Vector path later-ready: libSQL/Turso native vector options (e.g. `F32_BLOB` + vector index as an **implementation option**, not an MVP schema requirement)
7. **Vector / embeddings (deferred, optional, phase four / future agent only):**
   - Turso native vector columns/indexes **may** store **tenant-scoped** embeddings of: approved derived summaries, user-authored journal/conversation content, and curated knowledge.
   - **Never** embed canonical **raw measurements** by default.
   - Schema shape: **`retrieval_documents` parent** + **`vector_embeddings` real FK**; typed source links with CHECK exactly one (or require typed links before implementation)—**no** generic `source_kind`/`source_id` pointer.
   - Required metadata: embedding model/provider/version, dimension, source content hash, sensitivity, consent, created time, delete/rebuild lineage.
   - Deterministic SQL remains **source of truth**. All scores, recommendations, dashboard, and export work **without** embeddings or models.
   - **Tenant predicate mandatory** before/with retrieval; evaluate filtered ANN in a spike; rebuild on embedding-version changes; delete embeddings with source data.
8. **Local relational tests** may use SQLite; **vector integration tests** use **libSQL/Turso** (not plain SQLite-only assumptions).

## Consequences

### Positive

- Clear production choice; fewer open product decisions after docs freeze
- Simple mental model; transactional job claims
- Local offline-friendly development with SQLite/libSQL
- Optional vectors can colocate with tenant data later without a second OLTP product in MVP

### Negative

- Phase zero must still prove driver/migration/concurrency fitness; a proven blocker reopens the ADR
- High-cardinality intraday samples may stress SQLite-class stores (mitigate with volume policy first)
- Multi-region write patterns limited vs some distributed SQL systems
- libSQL has **no RLS**; application composite tenant auth is mandatory ([security.md](../architecture/security.md))

### Neutral

- Analytics warehouse remains a post-MVP decision
- Vector schema is later-ready documentation, not MVP DDL

## Reversal conditions

**Reopen this ADR only if phase zero (or production evidence) proves a hard blocker**, for example:

1. No acceptable Python + SQLAlchemy 2 + Alembic path for Turso with migration safety, **or**
2. Concurrency model cannot support API + worker job leases without corruption under realistic load, **or**
3. Volume proves minute-level samples exceed cost/perf budget even after downsampling/cold storage strategy, **or**
4. Encryption-at-rest / backup / restore requirements cannot be met on the Turso path without an unacceptable redesign.

Fallback candidates (in order): managed PostgreSQL; then reassess.

If only volume is the issue, first reverse the **timeseries storage strategy** (downsample, cold storage) before abandoning the operational DB choice.

Vector features failing a spike do **not** reverse the operational store decision; they defer or redesign the optional embedding boundary only.

## Related

- [../architecture/data-model.md](../architecture/data-model.md)
- [../architecture/operations.md](../architecture/operations.md)
- [../architecture/repository-and-services.md](../architecture/repository-and-services.md)
- [../roadmap.md](../roadmap.md)
- [../references.md](../references.md)
