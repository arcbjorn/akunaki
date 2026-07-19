"""Privacy deletion pipeline: states and ordering rules.

Pure: no I/O, no clock. The ordering is a safety property, not bookkeeping —
jobs must be cancelled **before** rows are scrubbed, or an in-flight sync could
re-insert data that was just deleted.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DeletionStatus(StrEnum):
    """Deletion pipeline state.

    Ordering is documented in security.md: cancel work, then scrub rows, then
    schedule backup expiry, then complete.
    """

    REQUESTED = "requested"
    JOBS_CANCELLED = "jobs_cancelled"
    ROWS_SCRUBBED = "rows_scrubbed"
    BACKUPS_SCHEDULED = "backups_scheduled"
    COMPLETED = "completed"
    FAILED = "failed"


# The only forward transitions the pipeline permits. A stage may not be
# skipped: scrubbing before cancelling would let a running job re-insert
# deleted rows.
_ALLOWED: dict[DeletionStatus, frozenset[DeletionStatus]] = {
    DeletionStatus.REQUESTED: frozenset({DeletionStatus.JOBS_CANCELLED, DeletionStatus.FAILED}),
    DeletionStatus.JOBS_CANCELLED: frozenset({DeletionStatus.ROWS_SCRUBBED, DeletionStatus.FAILED}),
    DeletionStatus.ROWS_SCRUBBED: frozenset(
        {DeletionStatus.BACKUPS_SCHEDULED, DeletionStatus.FAILED}
    ),
    DeletionStatus.BACKUPS_SCHEDULED: frozenset({DeletionStatus.COMPLETED, DeletionStatus.FAILED}),
    # Terminal.
    DeletionStatus.COMPLETED: frozenset(),
    DeletionStatus.FAILED: frozenset(),
}


class DeletionOrderError(Exception):
    """An illegal pipeline transition was attempted."""


def require_transition(current: DeletionStatus, target: DeletionStatus) -> None:
    """Raise unless ``current -> target`` is a permitted transition."""
    if target not in _ALLOWED[current]:
        msg = f"cannot move deletion from {current.value!r} to {target.value!r}"
        raise DeletionOrderError(msg)


def is_terminal(status: DeletionStatus) -> bool:
    """True when no further transition is possible."""
    return not _ALLOWED[status]


@dataclass(frozen=True, slots=True)
class ScrubCounts:
    """Per-class counts of scrubbed rows.

    Counts only: the completion proof must carry no identity and no health
    values, so this is the entire payload an auditor sees.
    """

    connections: int = 0
    connection_secrets: int = 0
    oauth_states: int = 0
    raw_payloads: int = 0
    raw_revisions: int = 0
    raw_objects: int = 0
    sync_runs: int = 0
    sync_cursors: int = 0
    facts: int = 0
    jobs_cancelled: int = 0

    def as_dict(self) -> dict[str, int]:
        """Serializable counts for the completion proof."""
        return {
            "connections": self.connections,
            "connection_secrets": self.connection_secrets,
            "oauth_states": self.oauth_states,
            "raw_payloads": self.raw_payloads,
            "raw_revisions": self.raw_revisions,
            "raw_objects": self.raw_objects,
            "sync_runs": self.sync_runs,
            "sync_cursors": self.sync_cursors,
            "facts": self.facts,
            "jobs_cancelled": self.jobs_cancelled,
        }

    @property
    def total_rows(self) -> int:
        """Total rows removed, excluding cancelled jobs."""
        return sum(self.as_dict().values()) - self.jobs_cancelled
