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
from akunaki.domain.audit import AUDIT_CHAIN_CHECK
from akunaki.domain.jobs import AUDIT_VERIFY_JOB_TYPE, JobClaim

logger = logging.getLogger("akunaki.audit")

__all__ = ["AUDIT_VERIFY_JOB_TYPE", "AuditChainVerifier", "AuditVerifyHandler"]


class AuditChainVerifier(Protocol):
    """Port: verify the stored audit chain."""

    def verify(self) -> int | None:
        """Return the ``seq`` of the first tampered event, or None when intact."""
        ...


class CheckResultSink(Protocol):
    """Port: persist the latest result of a named system check."""

    def record(self, *, name: str, ok: bool, detail: str | None, now: datetime) -> None:
        """Overwrite the named check's latest result."""
        ...


class AuditVerifyHandler:
    """Verify the audit chain and publish the result."""

    def __init__(
        self,
        *,
        audit: AuditChainVerifier,
        checks: CheckResultSink | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._audit = audit
        self._checks = checks
        self._clock = clock

    def _persist(self, *, ok: bool, detail: str | None, now: datetime) -> None:
        """Record the verdict where another process can read it.

        The gauge lives in **this** process's registry, and the worker serves
        no metrics endpoint — so a gauge alone reaches nobody. Persisting is
        what makes `/readyz` (served by the API) able to report the verdict.

        Never raises: the verification already happened and the gauge already
        carries it; failing here would turn a successful check into a retry.
        """
        if self._checks is None:
            return
        try:
            self._checks.record(
                name=AUDIT_CHAIN_CHECK,
                ok=ok,
                detail=detail,
                now=now,
            )
        except Exception:
            logger.exception("failed to persist audit chain check result")

    def __call__(self, claim: JobClaim) -> None:
        """Run one verification pass."""
        tampered_seq = self._audit.verify()
        now = self._clock()
        AUDIT_CHAIN_VERIFIED_AT.set(now.timestamp())

        if tampered_seq is None:
            AUDIT_CHAIN_INTACT.set(1.0)
            self._persist(ok=True, detail=None, now=now)
            logger.info("audit chain verified intact")
            return

        AUDIT_CHAIN_INTACT.set(0.0)
        self._persist(ok=False, detail=f"tampered_seq={tampered_seq}", now=now)
        # Logged at error so it surfaces without the metric, and carries the
        # position rather than the content: the break is the finding, and the
        # event's fields are not for a log line.
        logger.error(
            "audit chain verification failed",
            extra={"tampered_seq": tampered_seq, "job_id": claim.job_id},
        )
