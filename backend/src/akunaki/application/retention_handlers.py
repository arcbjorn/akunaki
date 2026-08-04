"""Scheduled expiry sweep for short-lived credential material.

Sessions, OAuth/PKCE states, login states, and tool confirmations all carry an
``expires_at`` and all stop being usable the moment it passes. Nothing deleted
them, so every expired row kept its stored secrets — hashed session tokens and
CSRF secrets, sealed PKCE verifiers, confirmation token hashes — indefinitely.
Each table already had an ``expires_at`` index built for a sweep that was never
wired.

This is that sweep. It deletes only rows past their own expiry, so it can never
remove something still in use: an expired row cannot authenticate, cannot
complete an OAuth exchange, and cannot authorize a mutation.

Leader-gated like the reconcile sweep, so one process cleans up however many
workers are running.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from akunaki.domain.jobs import RETENTION_SWEEP_JOB_TYPE, JobClaim

logger = logging.getLogger("akunaki.retention")

__all__ = ["RETENTION_SWEEP_JOB_TYPE", "ExpiringStore", "RetentionSweepHandler"]


class ExpiringStore(Protocol):
    """Port: a store holding rows that stop being usable at ``expires_at``."""

    def purge_expired(self, *, now: datetime) -> int:
        """Delete rows past their expiry; return how many were removed."""
        ...


class RetentionSweepHandler:
    """Delete expired credential material across every short-lived store."""

    def __init__(
        self,
        *,
        stores: dict[str, ExpiringStore],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._stores = stores
        self._clock = clock

    def __call__(self, claim: JobClaim) -> None:
        """Sweep every registered store.

        One store failing must not stop the others: a sweep that aborts halfway
        would leave the remaining stores un-purged until the next tick, and the
        failure is already visible in the log. The job still raises at the end
        so the worker's retry policy sees it.
        """
        now = self._clock()
        removed: dict[str, int] = {}
        failures: list[str] = []

        for name, store in sorted(self._stores.items()):
            try:
                removed[name] = store.purge_expired(now=now)
            except Exception:
                failures.append(name)
                logger.exception("retention sweep failed for a store", extra={"store": name})

        logger.info(
            "retention sweep complete",
            extra={"removed": removed, "failed_stores": failures, "job_id": claim.job_id},
        )
        if failures:
            msg = f"retention sweep failed for: {', '.join(failures)}"
            raise RuntimeError(msg)
