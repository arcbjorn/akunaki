"""Scheduled audit-chain verification.

A tamper-evident chain that nobody checks is theatre: the hashes only detect
tampering if something recomputes them. This is the detector — a leader-gated
periodic job that walks the chain and publishes the verdict as a metric, so an
operator alerts on it rather than remembering to look.

Runs on the worker, not on a request path. Verification is O(chain) and the
audit table only grows, so putting it behind an endpoint would hand any caller
an unbounded scan; behind the leader lease it runs once per interval on one
process regardless of how many workers are up.

A detected break does **not** fail the job. Tampering is not a transient error
that a retry could fix — retrying would just burn the attempt budget and then
dead-letter, losing the signal. The gauge stays at 0 until a verification
passes, which is exactly the alert condition.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from akunaki.application.metrics import AUDIT_CHAIN_INTACT, AUDIT_CHAIN_VERIFIED_AT
from akunaki.domain.jobs import AUDIT_VERIFY_JOB_TYPE, JobClaim

logger = logging.getLogger("akunaki.audit")

__all__ = ["AUDIT_VERIFY_JOB_TYPE", "AuditChainVerifier", "AuditVerifyHandler"]


class AuditChainVerifier(Protocol):
    """Port: verify the stored audit chain."""

    def verify(self) -> int | None:
        """Return the ``seq`` of the first tampered event, or None when intact."""
        ...


class AuditVerifyHandler:
    """Verify the audit chain and publish the result."""

    def __init__(
        self,
        *,
        audit: AuditChainVerifier,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._audit = audit
        self._clock = clock

    def __call__(self, claim: JobClaim) -> None:
        """Run one verification pass."""
        tampered_seq = self._audit.verify()
        now = self._clock()
        AUDIT_CHAIN_VERIFIED_AT.set(now.timestamp())

        if tampered_seq is None:
            AUDIT_CHAIN_INTACT.set(1.0)
            logger.info("audit chain verified intact")
            return

        AUDIT_CHAIN_INTACT.set(0.0)
        # Logged at error so it surfaces without the metric, and carries the
        # position rather than the content: the break is the finding, and the
        # event's fields are not for a log line.
        logger.error(
            "audit chain verification failed",
            extra={"tampered_seq": tampered_seq, "job_id": claim.job_id},
        )
