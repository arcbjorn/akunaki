"""Data-quality finding rules (pure domain).

A finding is a **standing** condition the user can usually act on, distinct
from ``/v1/today``'s per-day gaps: "reconnect Oura" versus "this day has no
HRV".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from akunaki.domain.data_quality import (
    STALE_AFTER,
    ConnectionState,
    FindingCode,
    Severity,
    connection_findings,
)

NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)


def _state(
    *,
    provider: str = "oura",
    status: str = "active",
    last_success_at: datetime | None = NOW,
    consecutive_failures: int = 0,
) -> ConnectionState:
    return ConnectionState(
        provider=provider,
        status=status,
        last_success_at=last_success_at,
        consecutive_failures=consecutive_failures,
    )


def _codes(*states: ConnectionState) -> list[FindingCode]:
    return [f.code for f in connection_findings(list(states), now=NOW)]


def test_a_healthy_connection_raises_nothing() -> None:
    """No finding is the normal case; noise would train users to ignore it."""
    assert connection_findings([_state()], now=NOW) == ()


def test_no_connections_is_an_info_finding() -> None:
    """A new user has linked nothing — the UI needs to say why there is no data."""
    findings = connection_findings([], now=NOW)

    assert [f.code for f in findings] == [FindingCode.NO_CONNECTIONS]
    assert findings[0].severity is Severity.INFO
    assert findings[0].provider is None


def test_needs_reauth_is_an_error() -> None:
    """Data has stopped and will not resume without the user re-consenting."""
    findings = connection_findings([_state(status="needs_reauth")], now=NOW)

    assert findings[0].code is FindingCode.NEEDS_REAUTH
    assert findings[0].severity is Severity.ERROR
    assert findings[0].provider == "oura"


def test_revoked_is_informational_not_an_error() -> None:
    """The user chose this; calling it a fault would be wrong."""
    findings = connection_findings([_state(status="revoked")], now=NOW)

    assert [f.code for f in findings] == [FindingCode.REVOKED]
    assert findings[0].severity is Severity.INFO


def test_stale_sync_is_flagged_at_the_threshold() -> None:
    """Dead exactly at the threshold, not a tick later."""
    at_threshold = _state(last_success_at=NOW - STALE_AFTER)
    just_inside = _state(last_success_at=NOW - STALE_AFTER + timedelta(seconds=1))

    assert FindingCode.STALE_SYNC in _codes(at_threshold)
    assert _codes(just_inside) == []


def test_never_synced_is_distinct_from_stale() -> None:
    """ "Never worked" and "stopped working" need different copy."""
    assert _codes(_state(last_success_at=None)) == [FindingCode.NEVER_SYNCED]


def test_revoked_connection_is_not_also_called_stale() -> None:
    """A revoked connection is silent by design; staleness would be noise."""
    codes = _codes(_state(status="revoked", last_success_at=NOW - timedelta(days=30)))

    assert codes == [FindingCode.REVOKED]
    assert FindingCode.STALE_SYNC not in codes


def test_reauth_connection_is_not_also_called_stale() -> None:
    """The actionable finding is reconnecting; staleness is its consequence."""
    codes = _codes(_state(status="needs_reauth", last_success_at=NOW - timedelta(days=30)))

    assert codes == [FindingCode.NEEDS_REAUTH]


def test_repeated_failures_are_flagged() -> None:
    assert FindingCode.REPEATED_FAILURES in _codes(_state(consecutive_failures=3))


def test_a_single_failure_is_not_a_finding() -> None:
    """Transient failure is normal; a run of them is a standing problem."""
    assert _codes(_state(consecutive_failures=2)) == []


def test_one_connection_can_raise_several_findings() -> None:
    """Reasons are reported separately rather than collapsed.

    "Your data is stale" and "syncs keep failing" are different messages even
    when they share a cause.
    """
    codes = _codes(_state(last_success_at=NOW - timedelta(days=5), consecutive_failures=4))

    assert set(codes) == {FindingCode.STALE_SYNC, FindingCode.REPEATED_FAILURES}


def test_findings_are_ordered_most_severe_first() -> None:
    """A truncated list must show the problems that matter."""
    findings = connection_findings(
        [
            _state(provider="polar", last_success_at=NOW - timedelta(days=5)),
            _state(provider="oura", status="needs_reauth"),
        ],
        now=NOW,
    )

    assert [f.severity for f in findings] == [Severity.ERROR, Severity.WARNING]


def test_findings_are_deterministic() -> None:
    """Same inputs, same output — the caller can diff them across polls."""
    states = [
        _state(provider="oura", status="needs_reauth"),
        _state(provider="polar", last_success_at=None),
    ]

    assert connection_findings(states, now=NOW) == connection_findings(states, now=NOW)
