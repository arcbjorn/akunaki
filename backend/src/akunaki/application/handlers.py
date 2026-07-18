"""Job handler registry: maps a durable ``job_type`` to executable work.

Depends only on domain types. Handlers receive the winning claim and must be
idempotent: a lease can expire mid-execution and the job be retried by another
worker, so the same payload may be executed more than once.
"""

from __future__ import annotations

from collections.abc import Callable

from akunaki.domain.jobs import JobClaim

JobHandler = Callable[[JobClaim], None]

# Always-present no-op type. Migration 0003 backfills legacy rows to this type,
# so the runtime must be able to execute it without any product handler.
NOOP_JOB_TYPE = "system.noop"


def handle_noop(claim: JobClaim) -> None:
    """Succeed immediately; the durable lifecycle is the only observable effect."""


class HandlerRegistry:
    """Immutable-after-build mapping of job type to handler."""

    def __init__(self, handlers: dict[str, JobHandler] | None = None) -> None:
        self._handlers: dict[str, JobHandler] = dict(handlers or {})

    def register(self, job_type: str, handler: JobHandler) -> None:
        """Register ``handler`` for ``job_type``; duplicate types are rejected."""
        if not job_type.strip():
            msg = "job_type must be a non-empty string"
            raise ValueError(msg)
        if job_type in self._handlers:
            msg = f"handler already registered for job_type {job_type!r}"
            raise ValueError(msg)
        self._handlers[job_type] = handler

    def get(self, job_type: str) -> JobHandler | None:
        """Return the handler for ``job_type``, or None when unregistered."""
        return self._handlers.get(job_type)

    def job_types(self) -> frozenset[str]:
        """Return every registered job type."""
        return frozenset(self._handlers)


def default_registry() -> HandlerRegistry:
    """Registry containing only the built-in no-op handler."""
    return HandlerRegistry({NOOP_JOB_TYPE: handle_noop})
