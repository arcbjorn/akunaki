"""Port: record the lifecycle of a sync attempt.

Only the write half. The read side (``recent_for_tenant``) is an API concern
served straight from the adapter, so the sync handlers depend on the smallest
surface that lets them record what happened.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from akunaki.domain.sync_runs import SyncRunStatus, SyncRunTrigger

__all__ = ["SyncRunRecorderPort"]


class SyncRunRecorderPort(Protocol):
    """Open and close durable records of fetch executions."""

    def open(
        self,
        *,
        run_id: str,
        tenant_id: str,
        connection_id: str,
        trigger: SyncRunTrigger,
        stream: str | None,
        now: datetime,
    ) -> str:
        """Record a run as ``running`` before the fetch begins."""
        ...

    def close(
        self,
        *,
        run_id: str,
        status: SyncRunStatus,
        now: datetime,
        error_class: str | None = None,
    ) -> bool:
        """Finish a run; returns whether a ``running`` row was closed."""
        ...
