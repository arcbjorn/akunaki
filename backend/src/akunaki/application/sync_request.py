"""Manual sync: enqueue an incremental sync for one connection on request.

The scheduled reconcile sweep already catches gaps a missed webhook leaves, but
it runs on a 30-minute cadence. This is the "sync now" path — a user (or an
agent acting for them) asking for a refetch immediately.

Enqueues the **same** ``connection.incremental_sync`` job the webhook and sweep
paths use, so a manual sync is not a second code path with its own semantics:
it resumes from the stored cursor and dedupes on content hash exactly as an
automatic one does.

A connection in ``error`` syncs like an active one: the sweep retries those
on its own, and a manual request should not be refused what the schedule will
do anyway. A ``needs_reauth`` connection gets a **reauth probe** instead of a
sync — the one job that can lift the status without the user, by asking the
provider whether the refresh token still works (see ``reauth_handlers``).
Only ``revoked`` is refused: the user ended it, and only a new link undoes that.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from akunaki.domain.connections import ConnectionStatus, LinkedConnection
from akunaki.domain.jobs import INCREMENTAL_SYNC_JOB_TYPE, REAUTH_PROBE_JOB_TYPE
from akunaki.ports.jobs import JobEnqueuePort

__all__ = [
    "ConnectionLookupPort",
    "SyncRequestOutcome",
    "SyncRequestRejection",
    "SyncRequestService",
]

# Statuses from which a sync can actually succeed. ``error`` is here because the
# reconcile sweep already retries it; refusing a manual sync of what the
# schedule will attempt anyway only confuses the person asking.
_SYNCABLE = frozenset({ConnectionStatus.ACTIVE, ConnectionStatus.PENDING, ConnectionStatus.ERROR})
# Statuses a sync cannot fix but a refresh verdict can.
_PROBEABLE = frozenset({ConnectionStatus.NEEDS_REAUTH})


class SyncRequestRejection(StrEnum):
    """Why a manual sync was not enqueued."""

    NOT_FOUND = "not_found"
    """Unknown connection, or one the caller's tenant does not own."""

    NOT_SYNCABLE = "not_syncable"
    """Revoked: nothing short of a new link can make it sync."""


@dataclass(frozen=True, slots=True)
class SyncRequestOutcome:
    """What a manual sync request produced."""

    job_id: str
    created: bool
    """False when an identical sync was already in flight (deduped, not queued twice)."""
    probe: bool = False
    """True when the connection needed re-authorization and a reauth probe was
    queued in place of a sync. If the grant still works the probe restores the
    connection and queues its syncs itself."""


class ConnectionLookupPort(Protocol):
    """Port: resolve a connection's identity and status."""

    def get_connection(self, *, connection_id: str) -> LinkedConnection | None:
        """Return the connection, or None when unknown."""
        ...


class SyncRequestService:
    """Enqueue on-demand incremental syncs for a tenant's connections."""

    def __init__(
        self,
        *,
        connections: ConnectionLookupPort,
        jobs: JobEnqueuePort,
        new_id: Callable[[], str],
    ) -> None:
        self._connections = connections
        self._jobs = jobs
        self._new_id = new_id

    def request_sync(
        self,
        *,
        tenant_id: str,
        connection_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> SyncRequestOutcome | SyncRequestRejection:
        """Enqueue an incremental sync, or say why it was refused.

        The caller's ``idempotency_key`` is namespaced per connection, so a
        retried request (a double-clicked button, an agent retry) enqueues one
        job rather than a queue of duplicates. Because the key is released once
        the job settles, a later genuine "sync now" still works.

        A connection belonging to another tenant is reported as **not found**,
        indistinguishable from one that does not exist, so an id cannot be
        probed across tenants.
        """
        connection = self._connections.get_connection(connection_id=connection_id)
        if connection is None or connection.tenant_id != tenant_id:
            return SyncRequestRejection.NOT_FOUND
        if connection.status in _PROBEABLE:
            # Keyed like the sweep's own probe, so a manual request and a
            # scheduled one collapse onto a single job rather than racing.
            probe = self._jobs.enqueue_job(
                job_id=self._new_id(),
                tenant_id=tenant_id,
                job_type=REAUTH_PROBE_JOB_TYPE,
                payload_json=json.dumps({"connection_id": connection_id}, sort_keys=True),
                now=now,
                idempotency_key=f"reauth_probe:{connection_id}",
            )
            return SyncRequestOutcome(job_id=probe.job_id, created=probe.created, probe=True)
        if connection.status not in _SYNCABLE:
            return SyncRequestRejection.NOT_SYNCABLE

        enqueued = self._jobs.enqueue_job(
            job_id=self._new_id(),
            tenant_id=tenant_id,
            job_type=INCREMENTAL_SYNC_JOB_TYPE,
            payload_json=json.dumps({"connection_id": connection_id}, sort_keys=True),
            now=now,
            idempotency_key=f"manual_sync:{connection_id}:{idempotency_key}",
        )
        return SyncRequestOutcome(job_id=enqueued.job_id, created=enqueued.created)
