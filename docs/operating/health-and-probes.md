# Health endpoints and probes

**Status:** Describes shipped code (`backend/src/akunaki/api/routes/health.py`, `backend/src/akunaki/api/routes/ready.py`)

**Last reviewed:** 2026-08-15

Two endpoints, both unauthenticated, both always mounted, both read-only — a
probe hitting either never perturbs state.

---

## `/healthz` — liveness

Answers: *is this process up, and can it reach the database?*

```
GET /healthz
→ 200
{
  "status": "ok",
  "service": "akunaki-api",
  "database_ready": true,
  "models_required": false
}
```

**It always returns `200`.** When the database probe (`SELECT 1`) fails,
`status` becomes `"degraded"` and `database_ready` becomes `false`, but the
status code does not change. This is the correct behaviour for a liveness
probe: a database outage should not cause the orchestrator to kill and restart a
perfectly healthy process — restarting it will not bring the database back.

Use it as a **liveness probe**. Do not use it as a readiness probe: it will
happily report `200` while the schema is behind head and the service cannot do
useful work.

`models_required` is always `false` — the core API never requires a model
provider. It is a contract assertion, not a variable.

---

## `/readyz` — readiness

Answers: *is this deployment actually able to do work?*

```
GET /readyz
→ 200 (ready) or 503 (not ready)
{
  "ready": true,
  "database_ready": true,
  "migration": {"at_head": true, "db_revision": "...", "code_head": "..."},
  "queue": {"ready": 0, "leased": 1, "dead_letter": 0},
  "audit": {
    "events": 42,
    "last_event_at": "...",
    "chain_intact": true,
    "chain_checked_at": "..."
  },
  "leader_held": true
}
```

### It returns 503, not 200-with-a-flag

This is the property that makes it directly usable as a probe target:

```python
ready = database_ready and at_head
if not ready:
    response.status_code = 503
```

When the database is unreachable **or** the schema is behind the code's
migration head, the response is **HTTP 503**. It is not a `200` carrying
`"ready": false`.

Two consequences:

- **Safe as a Kubernetes readiness probe** with no custom parsing. The default
  HTTP-GET probe treats 2xx/3xx as success and everything else as failure, so
  `httpGet: /readyz` does the right thing out of the box. A pod whose schema is
  behind head is pulled from the service endpoints instead of serving traffic
  against a schema it does not match.
- **Safe as a blackbox monitoring target.** A blackbox exporter or uptime check
  that asserts on status code alone catches both failure modes. You do not need
  a JSON-body assertion to detect an unmigrated deployment.

The JSON body is still worth scraping for a dashboard — it tells you *which*
condition failed and carries the reported-only fields below.

### What gates readiness, and what does not

| Field | Gates `ready`? | Notes |
|-------|---------------|-------|
| `database_ready` | **Yes** | A `SELECT 1` probe |
| `migration.at_head` | **Yes** | DB revision equals the code's head |
| `queue` | No | Reported for dashboards |
| `leader_held` | No | Reported for dashboards |
| `audit` | No | Reported for dashboards |

Queue depth and leader presence are deliberately **not** gating. An idle queue
is not an unready service, and a momentary leaderless gap between worker deploys
is not either — gating on them would flap your service endpoints for
non-conditions.

### Reading the reported fields

**`queue`** — counts of jobs by status: `ready` (waiting), `leased` (in
progress), `dead_letter` (exhausted retries). A growing `ready` count with a
zero `leased` count means no worker is consuming. A non-zero `dead_letter` is
the signal worth alerting on; it never drains by itself.

**`leader_held`** — whether a worker currently holds the `core-reaper` lease.
Persistently `false` means the scheduled reconcile, audit-verify, and retention
sweeps are not running. Since the worker serves no HTTP endpoint of its own,
this field plus `queue` is how you monitor the worker at all.

**`audit`** — `events` is the chain length and `last_event_at` its newest entry.
The value to watch is `last_event_at` going stale *while audited actions are
still occurring*: that means the trail stopped being written, which no counter
would reveal.

`chain_intact` is the **stored verdict of the scheduled hourly verification
job**, not a check performed by the probe — verifying is O(chain), so a probe
must never trigger it. `null` means it has never run, which is not a failure:
unknown and tampered must be distinguishable. A stale `chain_checked_at` means
the verification job stopped running; check `leader_held`.

When the database is down, or up but behind head, the unknown blocks are
reported as zeros rather than raising — the audit block is only populated once
the schema is at head, because a DB behind head may not have the `audit_events`
table yet, and a probe whose job is to report that must not itself fail on it.

---

## Suggested probe configuration

Expressed as behaviour rather than as manifests for any particular
orchestrator:

| Probe | Target | Success | Notes |
|-------|--------|---------|-------|
| Liveness | `GET /healthz` | Status `200` | Never gate on the body; it is always `200` while the process lives |
| Readiness | `GET /readyz` | Status `200` | The `503` does the work |
| Startup | `GET /readyz` | Status `200` | Give it enough failures to cover the migration job |

A startup probe on `/readyz` is worth configuring if migrations run as an init
container: it holds the pod out of service until the schema catches up, without
a liveness probe killing the container in the meantime.

Both endpoints send `Cache-Control: no-store` (`/readyz`) so an intermediary
never serves a stale sample — a cached readiness answer is worse than none.

---

## `/metrics`

Not a health endpoint, but the third observability surface. It is mounted
**only** when `AKUNAKI_METRICS_ENABLED=true`, is unauthenticated, and serves the
Prometheus text format from the **API process's** registry.

The worker has its own in-process registry and no HTTP server — two processes
cannot share an in-memory registry, and the design does not add a push gateway
— so the worker is observed through `/readyz`'s `queue` and `leader_held`
fields instead.

The exposition is PHI-free by construction: counts and liveness gauges labelled
with bounded tokens, never a tenant id, user id, or health value. It still
describes internal operations, so expose the port to your scraper's network,
not the internet.
