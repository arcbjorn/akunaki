"""The scheduled audit-chain verification job.

A tamper-evident chain only detects tampering if something recomputes it. This
is that detector, so what matters is: it runs, it publishes a verdict either
way, and a detected break does not vanish into a retry.
"""

from __future__ import annotations

from datetime import UTC, datetime

from akunaki.application.audit_handlers import AUDIT_VERIFY_JOB_TYPE, AuditVerifyHandler
from akunaki.application.metrics import REGISTRY
from akunaki.domain.jobs import JobClaim, JobRole

T0 = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)


class _Chain:
    """A verifier with a fixed verdict, recording how often it was asked."""

    def __init__(self, verdict: int | None) -> None:
        self._verdict = verdict
        self.calls = 0

    def verify(self) -> int | None:
        self.calls += 1
        return self._verdict


def _claim() -> JobClaim:
    return JobClaim(
        job_id="audit-1",
        tenant_id="system",
        role=JobRole.CORE,
        job_type=AUDIT_VERIFY_JOB_TYPE,
        owner="worker-1",
        fence_token=1,
        leased_until="2026-08-04T13:00:00Z",
        attempts=1,
        max_attempts=5,
        payload_json="{}",
    )


def _gauge_value(name: str) -> float | None:
    """Read a gauge's current unlabelled value from the shared registry."""
    for line in REGISTRY.render().splitlines():
        if line.startswith(f"{name} "):
            return float(line.rsplit(" ", 1)[1])
    return None


def test_intact_chain_publishes_one() -> None:
    chain = _Chain(verdict=None)
    AuditVerifyHandler(audit=chain, clock=lambda: T0)(_claim())

    assert chain.calls == 1
    assert _gauge_value("akunaki_audit_chain_intact") == 1.0
    assert _gauge_value("akunaki_audit_chain_verified_timestamp_seconds") == T0.timestamp()


def test_tampered_chain_publishes_zero() -> None:
    """The alert condition: the gauge drops and stays down until a pass."""
    AuditVerifyHandler(audit=_Chain(verdict=None), clock=lambda: T0)(_claim())
    assert _gauge_value("akunaki_audit_chain_intact") == 1.0

    AuditVerifyHandler(audit=_Chain(verdict=42), clock=lambda: T0)(_claim())

    assert _gauge_value("akunaki_audit_chain_intact") == 0.0


def test_detected_tampering_does_not_raise() -> None:
    """Tampering is not transient, so failing the job would lose the signal.

    A raise would burn the attempt budget and dead-letter, turning a standing
    alert into a one-off error nobody sees.
    """
    handler = AuditVerifyHandler(audit=_Chain(verdict=7), clock=lambda: T0)

    handler(_claim())  # must not raise

    assert _gauge_value("akunaki_audit_chain_intact") == 0.0


def test_verification_timestamp_advances_on_every_run() -> None:
    """A stale timestamp is itself an alert: the verifier stopped running."""
    later = datetime(2026, 8, 4, 13, 0, 0, tzinfo=UTC)
    AuditVerifyHandler(audit=_Chain(verdict=None), clock=lambda: T0)(_claim())
    AuditVerifyHandler(audit=_Chain(verdict=None), clock=lambda: later)(_claim())

    assert _gauge_value("akunaki_audit_chain_verified_timestamp_seconds") == later.timestamp()
