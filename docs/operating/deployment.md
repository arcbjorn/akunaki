# Deployment

**Status:** Describes shipped code (`backend/Dockerfile`, `backend/src/akunaki/api/__main__.py`, `backend/src/akunaki/worker/__main__.py`)

**Last reviewed:** 2026-08-15

---

## The container contract

`backend/Dockerfile` builds **one image that serves three commands**. This is
deliberate: the API, the worker, and the migration runner can never drift onto
different code or dependency sets, because there is only one artifact.

| Role | Command | Notes |
|------|---------|-------|
| API | `python -m akunaki.api` | The image's default `CMD`. Set `AKUNAKI_API_HOST=0.0.0.0`. |
| Worker | `python -m akunaki.worker` | Long-running job claim loop. No listening port. |
| Migrations | `alembic upgrade head` | Run to completion **before** the other two. Init container or pre-deploy job. |

Facts a deployment must encode:

| Property | Value | Where it comes from |
|----------|-------|---------------------|
| Runtime user | uid **10001**, gid 10001, name `akunaki` | `Dockerfile` — a fixed uid so a mounted volume's ownership survives image rebuilds |
| Working directory | `/app` | `WORKDIR /app` |
| Virtualenv | `/app/.venv`, already on `PATH` | `ENV PATH="/app/.venv/bin:$PATH"` |
| API port | **8000** by default | `AKUNAKI_API_PORT` |
| Bind address | `127.0.0.1` by default | `AKUNAKI_API_HOST` — **must be overridden in a container** |
| Python | pinned to exactly 3.13.14 | `requires-python = ">=3.13.14,<3.14"`; 3.14 + sqlalchemy-libsql segfaulted |

### `AKUNAKI_API_HOST=0.0.0.0` is required in a container

The default bind address is loopback, so a bare local run exposes nothing. In a
container, loopback means nothing outside the container can reach the API —
including the orchestrator's health probes. The symptom is a container that
starts cleanly, logs normally, and never passes a readiness check.

Set `AKUNAKI_API_HOST=0.0.0.0` on the API role only. The worker binds nothing
and ignores it.

### The image contains the operator scripts

`scripts/` is copied into the image at `/app/scripts`, so operator acts run
inside a container with the deployed code and its exact dependencies. That is
how you mint a service token — see
[tools-api.md](tools-api.md#minting-listing-and-revoking-tokens)
and [bootstrap.md](bootstrap.md).

The image is built from a `.dockerignore` allow-list: only `pyproject.toml`,
`uv.lock`, `README.md`, `alembic.ini`, `src/`, and `scripts/` enter it. Tests
and dev dependencies are not present, and no model/AI SDK is installed.

---

## The database is a single-writer file

`AKUNAKI_DATABASE_URL` accepts **only local `sqlite+libsql:` URLs**. Hostnames,
credentials, ports, query strings, and fragments are rejected by a validator at
settings load (`config.py`, `_is_local_libsql_url`), so a remote Turso URL will
not start the process — it raises at boot rather than silently falling back.

The store is therefore a file on a filesystem the processes share, in WAL mode
(applied once per file-backed engine in `adapters/db/engine.py`). Three
consequences, all mandatory:

**One API replica and one worker replica.** Two writers on one SQLite file
across a network filesystem is a corruption risk, not a scaling strategy. The
`busy_timeout` is set to 50 ms deliberately — contested transactions fail fast
and retry rather than queueing — which is tuned for in-process concurrency, not
for a second host.

**Never a rolling deploy.** A rolling update runs the old and new pods
simultaneously and gives you two writers against one file for the overlap. Use
a recreate strategy: stop the old instance, then start the new one. Accept the
brief downtime; it is the price of the storage choice.

**The volume must survive the pod.** The database file, its `-wal`, and its
`-shm` sidecars are the entire state of the system: connections, sealed OAuth
tokens, ingested facts, the audit chain. Mount persistent storage and make sure
it is writable by uid 10001.

If the URL points at a path whose parent directory does not exist, the engine
creates the parent at connect time (`_ensure_local_parent_dir`). It does not
create the volume.

---

## Startup order

The order is enforced by failure, not by orchestration, so encode it yourself:

```
1. alembic upgrade head      → run to completion, exit 0
2. python -m akunaki.api     → and/or
   python -m akunaki.worker
```

**Migrations first.** The API will start against a schema behind head, but
`/readyz` returns **503** until the schema matches (see
[health-and-probes.md](health-and-probes.md)), so a correctly probed deployment
never takes traffic in that state. The worker is stricter: it probes the
database at boot and exits with code **1** and
`akunaki.worker: database not ready; aborting boot` if it cannot connect.

Alembic reads the same configuration as the application — `migrations/env.py`
builds the URL from `Settings`, so `AKUNAKI_DATABASE_URL` must be set
identically on the migration job. It does not read `sqlalchemy.url` from
`alembic.ini` at runtime.

---

## The worker

`python -m akunaki.worker` runs the durable job claim loop. It is not optional
if you have connectors linked: it is what performs syncs, reconciliation,
retention sweeps, and audit-chain verification.

One worker holds a leader lease (`core-reaper`) and enqueues three scheduled
jobs:

| Schedule | Interval | Purpose |
|----------|----------|---------|
| Reconcile sweep | 30 minutes | Re-syncs connections that have gone stale |
| Audit chain verify | 1 hour | Verifies the audit hash chain; the verdict surfaces on `/readyz` |
| Retention sweep | 1 hour | Deletes expired sessions, OAuth/login states, and confirmations |

Shutdown is cooperative: `SIGINT` and `SIGTERM` set a stop flag and the loop
finishes its current job before exiting 0. Give the container a termination
grace period long enough for a job to finish rather than killing it mid-lease.

The worker keeps its own in-process metrics registry and **serves no HTTP
endpoint**. It is observed indirectly, through the `queue` and `leader_held`
fields on the API's `/readyz`.

---

## Logging

Both processes log structured JSON to stdout at `INFO`. The API's format
includes a per-request correlation id:

```json
{"ts":"...","level":"INFO","logger":"...","request_id":"...","msg":"..."}
```

Outside a request the id is `-`, so startup lines are still well-formed. The
same id is echoed on the response as the `x-request-id` header, and a client
may supply its own. Ship stdout to your log system; there is no log file and no
log configuration to mount.
