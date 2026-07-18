# Phase Zero evidence: local libSQL durable job lifecycle and leader fencing

**Date:** 2026-07-18

**Status:** Partial — the **local** file-backed libSQL repository implements the atomic durable execution lifecycle and leader fencing, and the **worker runtime** (claim → execute → heartbeat → settle, retry classification and capped backoff, leader-gated reaping) is implemented and proven under concurrent runtimes; **product job handlers**, **atomic domain side-effect fencing (UoW)**, sustained multi-**process** fleet load, and **Turso Cloud** are **not** implemented

**Authoritative context:** [repository-and-services.md](../architecture/repository-and-services.md) job protocol, [testing.md](../testing.md) integration expectations, [ADR 0003](../adr/0003-libsql-operational-store.md)

---

## Exact protocol implemented

Atomic claim uses **candidate discovery + conditional compare-and-swap**, never row-level lock idioms.

1. **Discover** (short read transaction): `status=ready`, `run_after <= now`, matching worker `role`, `attempts < max_attempts`, ordered by `priority ASC`, `created_at ASC`, `id ASC`. Capture `expected_fence_token` per candidate.
2. **Claim CAS** (separate short write transaction per candidate): conditional `UPDATE jobs` succeeds only when still `ready`, due, `fence_token` equals expected, `role` matches, and attempts remain. On win: set `leased`, increment `attempts` and `fence_token`, replace the matching `job_leases` row, and insert exactly one deterministic `(job_id, attempt_number)` `job_attempts` row with the current fence, owner, `running` status, and `started_at`, all in the same transaction. **Zero-row update → lose cleanly**; try next candidate / rediscover. Each CAS attempt runs in its own short transaction; discovery and CAS are never in the same transaction.
3. **Heartbeat job**: extend `leased_until` only when lease owner/fence/unexpired match **and** the jobs row remains `leased` with the same fence.
4. **Complete**: require the exact current owner and fence on an unexpired lease plus the matching `running` attempt; atomically mark that attempt `succeeded` with `finished_at`, mark the job `succeeded`, and delete only the exact lease. Stale or inconsistent state returns `false` with no mutation.
5. **Explicit failure**: require the same exact unexpired lease and matching `running` attempt. A retryable failure with attempts remaining marks the attempt `retry_scheduled`, records redacted error data and `finished_at`, sets the job `ready` with `run_after = now + retry_delay`, records `last_error_class`, increments the job fence exactly once, and deletes the exact lease. Nonretryable failures and exhausted attempts instead mark the attempt and job `dead_letter`, increment the fence once, delete the exact lease, and create the job's single `job_dead_letters` record. Stale, wrong, expired, or inconsistent input returns `None` with no mutation.
6. **Requeue expired** (per-row fenced CAS): for each candidate, conditional `UPDATE` rechecks `status=leased`, expected fence, `attempts < max_attempts`, a matching expired lease, and its matching `running` attempt; on one-row win increment the job fence, set the job `ready`, mark the attempt `lease_expired` with `error_class=worker_lease_expired` and `finished_at`, then delete that exact lease. Return **actual wins**, not discovery count.
7. **Dead-letter expired** (same fenced CAS shape): a leased job at max attempts with the matching expired lease and `running` attempt becomes `dead_letter`; the attempt becomes `dead_letter` with `error_class=worker_lease_expired`, the job fence increments once, the exact lease is deleted, and one `job_dead_letters` row is written.
8. **Leader lease**: named row in `leader_leases`; CAS acquire when free/expired (null owner/expiry or `leased_until <= now`); fence increments on takeover; heartbeat and validity checks require owner + fence + unexpired. Schema requires nonempty `lease_name` and owner/expiry both null or both non-null.
9. **`has_valid_job_lease`**: validity primitive (job status leased + owner + fence + unexpired matching lease). **Not** atomic domain side-effect fencing; that integrates with a later application unit of work.

Failure inputs reject naive time, empty identifiers or `error_class`, negative retry delay, and redacted messages over 500 characters. Retry scheduling is a durable repository primitive using the caller-provided delay; no runtime retry classification or backoff policy is claimed.

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

### Worker runtime invariants (execution policy over the repository)

| Invariant | Test coverage |
|-----------|---------------|
| Concurrent runtimes drain a queue with exactly-once handler execution | `test_competing_workers_execute_each_job_exactly_once` (24 jobs, 3 workers, independent engines, barrier start; one attempt row per job) |
| Concurrent reaper ticks yield a single leader; standbys never reap | `test_only_one_worker_holds_the_reaper_lease` (4 contenders; exactly one `core-reaper` lease row) |
| Runtime heartbeat guard blocks completion of a stolen lease | `test_heartbeat_observes_stolen_lease_and_blocks_completion` (asserts `complete_job` is never called) |
| Durable fence rejects completion when no heartbeat observed the theft | `test_repository_fence_rejects_completion_when_heartbeat_misses_theft` (asserts completion *was* attempted and refused) |
| Transient failure → fenced retry, re-claimable after delay; exhaustion → dead letter | `test_transient_failure_persists_retry_then_reclaims_and_succeeds`, `test_retries_exhaust_into_dead_letter` |
| Permanent failure / unregistered `job_type` dead-letters without exhausting attempts | `test_permanent_failure_dead_letters_on_first_attempt`, `test_unregistered_job_type_dead_letters_through_real_lifecycle` |
| Leader reaper requeues a crashed worker's expired lease, then runs the job | `test_leader_reaper_requeues_expired_lease_from_a_crashed_worker` |

The two stolen-lease tests assert on **distinct observables** (`complete_job` call count) so each fails only for its own mechanism; a disabled runtime guard is verified to fail the first test while the fence backstop still holds.

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
| Worker fleet (runtime) | 24 jobs, 3 `JobWorker` runtimes, one engine each, barrier start |
| Reaper leader (runtime) | 4 `JobWorker` runtimes ticking concurrently, barrier start |
| Stolen lease (runtime) | 1 job; reaper steals mid-handler; handler released on an explicit event (no sleeps) |
| Time source | Fixed timezone-aware `datetime` (no wall-clock flakiness) |
| Temp DBs | Under pytest `tmp_path` |

Concurrency tests do **not** use always-true branches, fairness assumptions, or test-level `database is locked` swallowing. Repository contention handling is solely responsible. Final multi-worker assertions require full unique claim coverage; both workers winning is **not** required (not a CAS guarantee). Every joined thread is asserted not alive after `join`.

---

## Honest local-only scope

| In scope | Out of scope (not claimed) |
|----------|----------------------------|
| Local file-backed `sqlite+libsql` (QueuePool) + in-memory StaticPool | Turso Cloud / remote auth |
| Jobs, leases, attempts, and dead letters (migrations through `20260713_0003`) | Sustained multi-**process** fleet under production load |
| `JobRepository` CAS claim and atomic execution-lifecycle transitions | Atomic domain side-effect fencing / application UoW |
| Worker runtime claim/heartbeat/settle loop + leader-gated reaper tick | Product job handlers and operator dead-letter UI |
| Runtime retry classification and capped backoff policy | Turso Cloud multi-client execution |
| Pure domain lifecycle/failure types + ports Protocol | Production multi-region leadership |
| Durable attempt history and one-to-one dead-letter records | Encryption / backup spikes |
| Integration tests on temp files (threads, independent engines) | |
| PRAGMA `foreign_keys` + `busy_timeout=50` + file WAL once | |

### libSQL lock-contention note

`PRAGMA busy_timeout=50` is set on every connection (and reported as set). **libsql-experimental** under concurrent writers may still raise `ValueError('database is locked')` without fully waiting like stdlib `sqlite3`. The repository applies a **bounded** retry **only** for that lock-contention class on short transactions (`_run_short_tx` budget **2.0 s**; `claim_next` outer polling budget **0.25 s**). Each retry opens a fresh Session; pooled checkouts (QueuePool) provide real DB-API connection reuse. Non-lock errors are never swallowed. This is a local driver quirk, not a relaxation of CAS semantics.

---

## Current 0003 lifecycle verification

**Date:** 2026-07-14

| Check | Command | Result |
|-------|---------|--------|
| Focused lifecycle module | `uv run pytest tests/test_job_lifecycle.py` | PASS (17 passed); covers attempt creation, completion, retry, dead letters, invalid claims, expiry, and races |
| Tests | `uv run pytest` | PASS (131 passed in 2.39 s) |
| Lint | `uv run ruff check .` | PASS (All checks passed!) |
| Format | `uv run ruff format --check .` | PASS (34 files already formatted) |
| Types | `uv run mypy src tests` | PASS (Success across 30 source files) |
| Import boundaries | `uv run lint-imports` | PASS (5 contracts kept) |
| Lock | `uv lock --check` | PASS (Resolved 67 packages) |
| Freshness | `uv tree --outdated` | Every direct dependency current; only transitive `pydantic-core` 2.46.4 is behind 2.47.0 because `pydantic` 2.13.4 requires 2.46.4 exactly |
| Audit | `uv run pip-audit` | PASS (No known vulnerabilities; local `akunaki` package skipped because it is not on PyPI) |
| Build | `uv build` | PASS (produced sdist and wheel) |
| Diff whitespace | `git diff --check` | PASS |

---

## Previous lease-foundation execution baseline

These commands were run from `backend/` on Python 3.13.14 for the `0002` lease-foundation baseline. The counts below predate the `0003` durable lifecycle work and are not presented as verification of that revision; current gate results belong in the final lifecycle verification record.

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
| `20260713_0003` | `jobs.job_type` / `last_error_class`, `job_attempts`, `job_dead_letters` |

The migration test exercises head → `20260713_0002` → head, preserves a legacy job, and verifies the `system.noop` backfill. Revisions `0001` and `0002` remain unchanged.

---

## What this evidence does *not* claim

- Sustained multi-**process** fleet under production load (concurrency is proven with in-process threads on independent engines, not a long-running or cross-host soak)
- Dead-letter operator UI or drain tooling
- Atomic domain side-effect fencing tied to application unit of work (`has_valid_job_lease` is validity-only)
- Turso Cloud connectivity or multi-region leader election
- Full product job types / handlers (only `system.noop` ships)
- Encryption-at-rest, volume spikes, vectors
- Multi-worker **fairness** (both workers always win claims)

---

## Related

- [phase-zero-turso-foundation.md](phase-zero-turso-foundation.md)
- [implementation-status.md](../implementation-status.md)
- [backend/README.md](../../backend/README.md)
- [architecture/repository-and-services.md](../architecture/repository-and-services.md)
