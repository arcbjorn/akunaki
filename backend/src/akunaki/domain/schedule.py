"""Periodic job scheduling (pure).

No I/O, no clock. A worker's leader runs a set of **periodic jobs** — e.g. the
reconciliation sweep — on fixed intervals. This module holds the deciding
function ``due_schedules``: given the schedule specs, when each last fired, and
now, it returns which are due. The worker (application) owns the enqueue and the
last-fired bookkeeping; the *when* decision lives here so it is testable without
a clock or a database.

Each spec's ``idempotency_key`` makes the enqueue safe even if two workers both
believe they are leader for an instant, or a worker crashes mid-fire: the job
repository dedupes on ``(tenant_id, idempotency_key)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    """One periodic job to enqueue on an interval."""

    job_type: str
    interval: timedelta
    tenant_id: str
    idempotency_key: str
    payload_json: str = "{}"

    def __post_init__(self) -> None:
        if self.interval <= timedelta(0):
            msg = "interval must be positive"
            raise ValueError(msg)
        if not self.job_type or not self.idempotency_key:
            msg = "job_type and idempotency_key must be non-empty"
            raise ValueError(msg)


def due_schedules(
    specs: list[ScheduleSpec],
    *,
    last_fired: dict[str, datetime],
    now: datetime,
) -> list[ScheduleSpec]:
    """Return the specs whose interval has elapsed since they last fired.

    A spec never fired before (absent from ``last_fired``) is due immediately, so
    a fresh worker enqueues its periodic jobs on the first tick rather than
    waiting out a full interval. Keyed by ``idempotency_key`` in ``last_fired``.
    """
    due: list[ScheduleSpec] = []
    for spec in specs:
        fired_at = last_fired.get(spec.idempotency_key)
        if fired_at is None or now - fired_at >= spec.interval:
            due.append(spec)
    return due
