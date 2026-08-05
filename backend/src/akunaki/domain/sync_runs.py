"""Closed vocabularies for a recorded sync attempt.

Mirrors the CHECK constraints on ``sync_runs`` so a value the database would
reject cannot be constructed in the first place — a typo becomes an import
error rather than an IntegrityError at the end of a fetch.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["SyncRunStatus", "SyncRunTrigger"]


class SyncRunTrigger(StrEnum):
    """What caused a sync attempt.

    Mirrors ``sync_run_trigger``. Worth recording because the same failure
    means different things by cause: a failing webhook points at the provider's
    delivery, a failing schedule at the connection.
    """

    SCHEDULE = "schedule"
    WEBHOOK = "webhook"
    MANUAL = "manual"
    RECONCILE = "reconcile"
    INITIAL = "initial"


class SyncRunStatus(StrEnum):
    """How a sync attempt ended.

    Mirrors ``sync_run_status``. ``RUNNING`` is the opening state and is never a
    close value: a run left running is an attempt that died mid-flight, which
    must stay distinguishable from one that finished.
    """

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"
