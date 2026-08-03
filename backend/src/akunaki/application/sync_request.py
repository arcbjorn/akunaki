"""Manual sync: enqueue an incremental sync for one connection on request.

The scheduled reconcile sweep already catches gaps a missed webhook leaves, but
it runs on a 30-minute cadence. This is the "sync now" path — a user (or an
agent acting for them) asking for a refetch immediately.

Enqueues the **same** ``connection.incremental_sync`` job the webhook and sweep
paths use, so a manual sync is not a second code path with its own semantics:
it resumes from the stored cursor and dedupes on content hash exactly as an
automatic one does.

Refuses a connection that cannot sync. A ``needs_reauth`` or ``revoked``
connection will not succeed until the user re-consents, so enqueuing would only
burn attempts and report a queued sync that is guaranteed to fail — the same
rule the reconcile sweep applies when it skips those connections.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from akunaki.domain.connections import ConnectionStatus, LinkedConnection
from akunaki.domain.jobs import INCREMENTAL_SYNC_JOB_TYPE
from akunaki.ports.jobs import JobRepositoryPort

__all__ = [
    "ConnectionLookupPort",
    "SyncRequestOutcome",
    "SyncRequestRejection",
    "SyncRequestService",
]

# Statuses from which a sync can actually succeed. Everything else needs user
# action first (re-consent) or is gone.
_SYNCABLE = frozenset({ConnectionStatus.ACTIVE, ConnectionStatus.PENDING})


class SyncRequestRejection(StrEnum):
    """Why a manual sync was not enqueued."""

    NOT_FOUND = "not_found"
    """Unknown connection, or one the caller's tenant does not own."""

    NOT_SYNCABLE = "not_syncable"
    """Linked but unable to sync until the user re-consents."""


@dataclass(frozen=True, slots=True)
class SyncRequestOutcome:
    """What a manual sync request produced."""

    job_id: str
    created: bool
    """False when an identical sync was already in flight (deduped, not queued twice)."""


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
        jobs: JobRepositoryPort,
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
