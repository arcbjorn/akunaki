# Phase Zero evidence: local libSQL durable job lease and leader fencing

**Date:** 2026-07-13

**Status:** Partial — **local** file-backed libSQL concurrency protocol implemented and tested; **worker claim loop**, **retries with backoff**, **attempt history tables**, **atomic domain side-effect fencing (UoW)**, and **Turso Cloud** are **not** implemented

**Authoritative context:** [repository-and-services.md](../architecture/repository-and-services.md) job protocol, [testing.md](../testing.md) integration expectations, [ADR 0003](../adr/0003-libsql-operational-store.md)

---

## Exact protocol implemented

Atomic claim uses **candidate discovery + conditional compare-and-swap**, never row-level lock idioms.

1. **Discover** (short read transaction): `status=ready`, `run_after <= now`, matching worker `role`, `attempts < max_attempts`, ordered by `priority ASC`, `created_at ASC`, `id ASC`. Capture `expected_fence_token` per candidate.
2. **Claim CAS** (separate short write transaction per candidate): conditional `UPDATE jobs` succeeds only when still `ready`, due, `fence_token` equals expected, `role` matches, and attempts remain. On win: set `leased`, `attempts = attempts + 1`, `fence_token = fence_token + 1`, then delete+insert matching `job_leases` row (`lease_owner`, `leased_until`, `fence_token`). **Zero-row update → lose cleanly**; try next candidate / rediscover. Each CAS attempt runs in its own short transaction; discovery and CAS are never in the same transaction.
3. **Heartbeat job**: extend `leased_until` only when lease owner/fence/unexpired match **and** the jobs row remains `leased` with the same fence.
4. **Complete**: succeed only with matching owner + fence on an unexpired lease; set `succeeded` and delete the lease matching job id + owner + fence.
5. **Requeue expired** (per-row fenced CAS): for each candidate, conditional `UPDATE` rechecks `status=leased`, expected fence, `attempts < max_attempts`, and a matching expired `job_leases` row with the same fence; on one-row win increment fence, set `ready`, delete that exact expired lease. Return **actual wins**, not discovery count.
6. **Dead-letter** (same fenced CAS shape): leased + expired lease + `attempts >= max_attempts` → `dead_letter` + fence increment + exact lease delete; return actual wins.
7. **Leader lease**: named row in `leader_leases`; CAS acquire when free/expired (null owner/expiry or `leased_until <= now`); fence increments on takeover; heartbeat and validity checks require owner + fence + unexpired. Schema requires nonempty `lease_name` and owner/expiry both null or both non-null.
8. **`has_valid_job_lease`**: validity primitive (job status leased + owner + fence + unexpired matching lease). **Not** atomic domain side-effect fencing; that integrates with a later application unit of work.

**Forbidden:** `SELECT … FOR UPDATE`, `SKIP LOCKED`, and SQLAlchemy `with_for_update`.

### Timestamp resolution

Canonical lease timestamps use **second precision** (`to_utc_rfc3339` drops microseconds). **`lease_ttl` must be at least one second** so a positive subsecond TTL cannot serialize to immediate expiry. Naive datetimes are rejected.

---

## Engine / pooling / pragma policy

| Setting | Exact value | Notes |
|---------|-------------|-------|
| In-memory URLs (`sqlite+libsql://`, `sqlite+libsql:///:memory:`) | `StaticPool` | Separate connections/sessions on one Engine share one in-memory DB |
| File-backed URLs | `QueuePool` (pool_size=5, max_overflow=5, pool_timeout=5) | Bounded connection pool; concurrent short CAS transactions reuse physical DB-API connections via pooled checkouts |
| `PRAGMA foreign_keys` | `ON` | Every new connection |
| `PRAGMA busy_timeout` | **50 ms** | Every new connection; short, bounded driver wait compatible with repository retry budget |
| `PRAGMA journal_mode=WAL` | Once per file Engine | First-connection hook only; **never** for in-memory |
| Repository short-tx lock retry budget | **2.0 s** | Only `database is locked` / `database is busy`; fresh session per retry (QueuePool provides connection reuse) |
| `claim_next` outer contention budget | **0.25 s** | Monotonic deadline for full discover-then-CAS-loop cycle; returns `None` when exhausted |

---

## Invariants proven

| Invariant | Test coverage |
|-----------|---------------|
| Due/role ordering (priority, created_at); future and wrong-role skipped | `test_discover_due_ordering_and_filters` |
| Exactly one winner for two clients, same expected fence | `test_exactly_one_winner_same_expected_fence` |
| Concurrent workers distribute many jobs; no duplicates; no silent loss | `test_concurrent_workers_distribute_many_jobs` (24 jobs, 2 independent engines; **no** fairness assertion; **no** test-level lock catch) |
| Loser retries next candidate via `claim_next` | `test_claim_next_loser_retries_next_candidate` |
| Heartbeat success; stale fence / wrong owner / expired reject | `test_heartbeat_success_and_stale_reject` |
| Shorter heartbeat horizon preserves expiry (job) | `test_heartbeat_shorter_horizon_preserves_expiry` |
| Longer heartbeat horizon extends (job) | `test_heartbeat_later_horizon_extends_job` |
| Shorter heartbeat horizon preserves expiry (leader) | `test_heartbeat_leader_horizon_never_shortens` |
| Longer heartbeat horizon extends (leader) | `test_heartbeat_leader_later_horizon_extends` |
| Complete success; stale / wrong owner / expired reject | `test_complete_success_and_rejects` |
| `has_valid_job_lease` current vs stale/expired/completed | `test_has_valid_job_lease_current_and_stale` |
| Expired requeue increments fence; prior token invalid | `test_requeue_expired_increments_fence_invalidates_stale` |
| Two concurrent reapers → combined wins exactly 1; fence +1 | `test_concurrent_reapers_exactly_one_win` |
| Max-attempt expiry dead-letters | `test_dead_letter_expired_at_max_attempts` |
| Two leader contenders → one winner; stale former leader cannot heartbeat | `test_two_leader_contenders_one_winner`, `test_expired_leader_takeover_increments_fence` |
| Leader owner/expiry pair + nonempty name constraints | `test_leader_lease_constraints_owner_expiry_pair`, model/schema agreement |
| Nested transaction / savepoint rollback isolates nested work | `test_nested_transaction_savepoint_behavior` |
| Migration upgrade head → downgrade `20260713_0001` → upgrade head | `test_migration_upgrade_downgrade_to_0001_upgrade` |
| ORM models agree with migration (lease tables, FKs, indexes, checks) | `test_lease_models_agree_with_migration`, schema agreement suite |
| Repository source has no lock idioms | `test_repository_source_has_no_for_update_or_skip_locked` |
| Timezone-aware times only; naive rejected | domain + claim path tests |
| Min 1s lease TTL; subsecond rejected; second-resolution documented | `test_lease_ttl_rejects_subsecond_and_zero`, `test_to_utc_rfc3339_second_resolution_truncates_subseconds` |
| `claim_next` validates owner/TTL/limit before discovery (empty queue) | `test_claim_next_validates_before_discovery_empty_queue` |
| busy_timeout PRAGMA = 50 ms | `test_busy_timeout_pragma_set` |
| File WAL once; memory engines do not require WAL | `test_file_wal_enabled_memory_does_not_require_wal` |
| In-memory StaticPool persists across session/connection checkouts | `test_memory_engine_persists_across_session_checkouts` (both memory URL forms) |
| Sequential QueuePool checkouts reuse one physical connection (event counter) | `test_queuepool_sequential_checkouts_reuse_connection` |
| Deterministic held-write-lock → claim_next returns None; succeeds after release | `test_claim_next_returns_none_under_bounded_contention` |
| Non-lock errors propagate through short-tx runner | `test_nonlock_error_propagates_through_short_tx` |
| Joined threads no longer alive after join | dual-claim, multi-worker, leader race, concurrent reaper, held-lock |

---

## Platform

| Field | Value |
|-------|-------|
| OS | macOS (Darwin), aarch64 (Apple Silicon) |
| Package manager | uv |
| Working directory | `backend/` |
| Python | **3.13.14** |
| Dialect | local `sqlite+libsql` via `sqlalchemy-libsql==0.2.0` |
| DB files | pytest `tmp_path` only |

---

## Stress shape (deterministic, bounded)

| Parameter | Value |
|-----------|-------|
| Dual claim race | 2 threads, 1 job, shared expected fence, `threading.Barrier` |
| Multi-worker distribution | 24 jobs, 2 independent engines/session factories, barrier start |
| Concurrent reaper race | 2 clients, 1 expired leased job, barrier start; sum of wins == 1 |
| Leader race | 2 contenders after pre-seeded expired lease row, barrier start |
| Time source | Fixed timezone-aware `datetime` (no wall-clock flakiness) |
| Temp DBs | Under pytest `tmp_path` |

Concurrency tests do **not** use always-true branches, fairness assumptions, or test-level `database is locked` swallowing. Repository contention handling is solely responsible. Final multi-worker assertions require full unique claim coverage; both workers winning is **not** required (not a CAS guarantee). Every joined thread is asserted not alive after `join`.

---

## Honest local-only scope

| In scope | Out of scope (not claimed) |
|----------|----------------------------|
| Local file-backed `sqlite+libsql` (QueuePool) + in-memory StaticPool | Turso Cloud / remote auth |
| Job + leader lease tables (migration `20260713_0002`) | Worker process claim loop |
| `JobRepository` CAS API + `has_valid_job_lease` validity primitive | Atomic domain side-effect fencing / application UoW |
| Domain types + ports Protocol | Retries with backoff policy |
| Integration tests on temp files | `job_attempts` history table |
| PRAGMA `foreign_keys` + `busy_timeout=50` + file WAL once | Production multi-region leadership |
| | Encryption / backup spikes |

### libSQL lock-contention note

`PRAGMA busy_timeout=50` is set on every connection (and reported as set). **libsql-experimental** under concurrent writers may still raise `ValueError('database is locked')` without fully waiting like stdlib `sqlite3`. The repository applies a **bounded** retry **only** for that lock-contention class on short transactions (`_run_short_tx` budget **2.0 s**; `claim_next` outer polling budget **0.25 s**). Each retry opens a fresh Session; pooled checkouts (QueuePool) provide real DB-API connection reuse. Non-lock errors are never swallowed. This is a local driver quirk, not a relaxation of CAS semantics.

---

## Exact executed results

Commands run from `backend/` after `uv sync --all-groups` on Python 3.13.14 (full gate re-run after this review-fix):

| Check | Command | Result |
|-------|---------|--------|
| Concurrency module (+ durations) | `uv run pytest tests/test_job_concurrency.py --durations=20` | PASS (35 passed in 1.36 s; slowest **0.39 s** `test_concurrent_workers_distribute_many_jobs`) |
| Repeated contention stress | shell loops: worker-race test x100, held-lock test x20 | PASS (exact worker race 100 of 100 passed; held-write-lock contract 20 of 20 passed) |
| Lint | `uv run ruff check .` | PASS (All checks passed!) |
| Format | `uv run ruff format --check .` | PASS (32 files already formatted) |
| Types | `uv run mypy src tests` | PASS (Success: no issues found in 29 source files) |
| Import boundaries | `uv run lint-imports` | PASS (5 kept, 0 broken) |
| Tests | `uv run pytest` | PASS (107 passed in 1.43 s) |
| Lock | `uv lock --check` | PASS (Resolved 67 packages) |
| Freshness | `uv tree --outdated` | Direct pins current; pydantic-core 2.46.4 (latest 2.47.0) expected per pydantic pin |
| Audit | `uv run pip-audit` | PASS (No known vulnerabilities found) |
| Build | `uv build --no-sources` | PASS (akunaki-0.1.0.tar.gz + akunaki-0.1.0-py3-none-any.whl) |
| Diff whitespace | `git diff --check` | PASS (no whitespace errors) |

### Schema revisions

| Revision | Contents |
|----------|----------|
| `20260713_0001` | `tenants`, `jobs` (unchanged) |
| `20260713_0002` | `job_leases`, `leader_leases` (owner/expiry pair + nonempty name checks) |

Downgrade of `0002` returns cleanly to `0001` (lease tables dropped; foundation tables retained).

---

## What this evidence does *not* claim

- Worker scheduler / claim loop process (`python -m akunaki.worker` remains a stub)
- Retry backoff, poison-message attempt history, dead-letter operator UI
- Atomic domain side-effect fencing tied to application unit of work (`has_valid_job_lease` is validity-only)
- Turso Cloud connectivity or multi-region leader election
- Full product job types / handlers
- Encryption-at-rest, volume spikes, vectors
- Multi-worker **fairness** (both workers always win claims)

---

## Related

- [phase-zero-turso-foundation.md](phase-zero-turso-foundation.md)
- [implementation-status.md](../implementation-status.md)
- [backend/README.md](../../backend/README.md)
- [architecture/repository-and-services.md](../architecture/repository-and-services.md)
