"""Provider-dispatch sync handler: route a job to its connection's handler."""

from __future__ import annotations

import pytest

from akunaki.application.sync_handlers import ProviderDispatchSyncHandler
from akunaki.domain.connections import (
    ConnectionStatus,
    LinkedConnection,
    Provider,
)
from akunaki.domain.jobs import JobClaim, JobRole
from akunaki.domain.retry import PermanentJobError


class _FakeConnections:
    def __init__(self, provider: Provider | None) -> None:
        self._provider = provider

    def get_connection(self, *, connection_id: str) -> LinkedConnection | None:
        if self._provider is None:
            return None
        return LinkedConnection(
            connection_id=connection_id,
            tenant_id="tenant-1",
            provider=self._provider,
            status=ConnectionStatus.ACTIVE,
            scopes=(),
            external_user_id=None,
        )


def _claim() -> JobClaim:
    return JobClaim(
        job_id="j1",
        tenant_id="tenant-1",
        role=JobRole.CORE,
        job_type="connection.initial_sync",
        owner="w1",
        fence_token=1,
        leased_until="2026-07-24T13:00:00Z",
        attempts=1,
        max_attempts=5,
        payload_json='{"connection_id":"conn-1"}',
    )


def test_dispatches_to_the_connections_provider() -> None:
    seen: list[str] = []
    handler = ProviderDispatchSyncHandler(
        connections=_FakeConnections(Provider.POLAR),
        handlers={
            ("oura", "sleep"): lambda _c: seen.append("oura"),
            ("polar", "workout"): lambda _c: seen.append("polar"),
        },
    )
    handler(_claim())
    assert seen == ["polar"]


def test_unknown_connection_is_permanent() -> None:
    handler = ProviderDispatchSyncHandler(
        connections=_FakeConnections(None),
        handlers={("oura", "sleep"): lambda _c: None},
    )
    with pytest.raises(PermanentJobError, match="not found"):
        handler(_claim())


def test_provider_without_handler_is_permanent() -> None:
    handler = ProviderDispatchSyncHandler(
        connections=_FakeConnections(Provider.GOOGLE_HEALTH),
        handlers={("oura", "sleep"): lambda _c: None},  # no google_health handler
    )
    with pytest.raises(PermanentJobError, match="no sync handler for provider"):
        handler(_claim())


def test_malformed_payload_is_permanent() -> None:
    handler = ProviderDispatchSyncHandler(
        connections=_FakeConnections(Provider.OURA),
        handlers={("oura", "sleep"): lambda _c: None},
    )
    bad = JobClaim(
        job_id="j1",
        tenant_id="tenant-1",
        role=JobRole.CORE,
        job_type="connection.initial_sync",
        owner="w1",
        fence_token=1,
        leased_until="2026-07-24T13:00:00Z",
        attempts=1,
        max_attempts=5,
        payload_json="{}",  # no connection_id
    )
    with pytest.raises(PermanentJobError):
        handler(bad)
