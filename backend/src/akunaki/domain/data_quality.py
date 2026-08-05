"""Data-quality findings: what is wrong with the *data*, not with a day.

``/v1/today`` already discloses per-day ``data_gaps`` — "this day has no HRV".
That is a property of one day and resolves itself when data arrives. A
data-quality finding is different: it is a **standing** condition across days
that the user can usually act on — a connection that needs re-consent, one that
has not synced in a week, one failing repeatedly.

Pure: no clock of its own, no I/O. The caller supplies ``now`` and the current
connection state, so the same inputs always yield the same findings.

Derived on read, not stored. The design lists a ``data_quality_findings`` table
but no detector or resolution lifecycle, and a persisted finding would need one
— it could otherwise outlive the condition it describes and tell the user to
fix something already fixed. Current state is the truth; deriving from it
cannot go stale.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

__all__ = [
    "STALE_AFTER",
    "DataQualityFinding",
    "FindingCode",
    "Severity",
    "connection_findings",
]

# How long without a successful sync before a connection is called stale. The
# reconcile sweep retries every 30 minutes and webhooks arrive sooner, so a day
# of silence means something is genuinely wrong rather than merely quiet.
STALE_AFTER = timedelta(days=1)

# Repeated failures that are not auth errors: transient once is normal, a run of
# them is a standing problem.
_FAILURE_STREAK = 3


class Severity(StrEnum):
    """How much the finding affects the data the engine can use."""

    INFO = "info"
    """Worth knowing; nothing is currently lost."""

    WARNING = "warning"
    """Data is going stale or partially missing."""

    ERROR = "error"
    """Data has stopped arriving and will not resume without user action."""


class FindingCode(StrEnum):
    """Closed vocabulary of findings.

    Closed on purpose: a client renders explanatory copy per code, so an
    invented code would surface as an untranslated string to the user.
    """

    NEEDS_REAUTH = "connection_needs_reauth"
    REVOKED = "connection_revoked"
    STALE_SYNC = "connection_stale_sync"
    NEVER_SYNCED = "connection_never_synced"
    REPEATED_FAILURES = "connection_repeated_failures"
    NO_CONNECTIONS = "no_connections_linked"


@dataclass(frozen=True, slots=True)
class DataQualityFinding:
    """One standing condition affecting the data.

    ``provider`` locates it for the user; there is deliberately no free-text
    message — a client owns the copy, and a server-authored sentence could not
    be localized and might leak vendor detail.
    """

    code: FindingCode
    severity: Severity
    provider: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectionState:
    """The current state of one connection, as findings care about it."""

    provider: str
    status: str
    last_success_at: datetime | None
    consecutive_failures: int


def connection_findings(
    states: list[ConnectionState],
    *,
    now: datetime,
) -> tuple[DataQualityFinding, ...]:
    """Derive the standing findings for a tenant's connections.

    Ordered most severe first, so a client rendering a truncated list shows the
    problems that matter. Within a severity, ordering follows the input so the
    result is deterministic for a deterministic caller.

    A connection can raise more than one finding — a revoked connection is also
    stale — but the reasons are reported separately rather than collapsed,
    because "reconnect" and "your data stops here" are different messages.
    """
    if not states:
        # Not an error: a new user has linked nothing yet. Reported so the UI
        # can say why there is no data rather than showing an empty chart.
        return (DataQualityFinding(code=FindingCode.NO_CONNECTIONS, severity=Severity.INFO),)

    findings: list[DataQualityFinding] = []
    for state in states:
        findings.extend(_findings_for(state, now=now))

    order = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
    return tuple(sorted(findings, key=lambda finding: order[finding.severity]))


def _findings_for(state: ConnectionState, *, now: datetime) -> list[DataQualityFinding]:
    """Every finding raised by one connection's current state."""
    findings: list[DataQualityFinding] = []

    if state.status == "needs_reauth":
        findings.append(
            DataQualityFinding(
                code=FindingCode.NEEDS_REAUTH,
                severity=Severity.ERROR,
                provider=state.provider,
            )
        )
    elif state.status == "revoked":
        # Not an error: the user chose this. Reported so a missing chart has an
        # explanation rather than looking like a fault.
        findings.append(
            DataQualityFinding(
                code=FindingCode.REVOKED,
                severity=Severity.INFO,
                provider=state.provider,
            )
        )

    # Staleness is only meaningful for a connection expected to be syncing. A
    # revoked one is silent by design, and telling the user it is stale would
    # be noise about a decision they made.
    if state.status not in {"revoked", "needs_reauth"}:
        if state.last_success_at is None:
            findings.append(
                DataQualityFinding(
                    code=FindingCode.NEVER_SYNCED,
                    severity=Severity.WARNING,
                    provider=state.provider,
                )
            )
        elif now - state.last_success_at >= STALE_AFTER:
            findings.append(
                DataQualityFinding(
                    code=FindingCode.STALE_SYNC,
                    severity=Severity.WARNING,
                    provider=state.provider,
                )
            )

    if state.consecutive_failures >= _FAILURE_STREAK:
        findings.append(
            DataQualityFinding(
                code=FindingCode.REPEATED_FAILURES,
                severity=Severity.WARNING,
                provider=state.provider,
            )
        )

    return findings
