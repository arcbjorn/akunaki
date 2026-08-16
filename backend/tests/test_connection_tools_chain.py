"""The lifecycle tools must be usable as a group, through the registry alone.

``connections.sync`` takes a ``connection_id``. The only tool that can supply
one is ``connections.list``. If the listing omits it, the mutating tool is
unreachable from the registry it lives in — invocable in principle, unusable in
practice, and no per-tool test would notice, because each tool passes its own.

So these drive the chain the way a caller does: resolve a tool **by name** from
the registry, invoke it with raw dict input, feed the output into the next
lookup. Nothing imports the tool factories directly, and nothing goes over
HTTP — the defect these exist for lives in the registry contract, not in either
transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pytest

from akunaki.application.connections_surface import ConnectionsSurfaceService, ConnectionSummary
from akunaki.application.sync_request import SyncRequestService
from akunaki.application.tool_registry import ToolContext, ToolRegistry
from akunaki.application.tools.connections import register_connection_tools
from akunaki.domain.connections import ConnectionStatus, LinkedConnection, Provider
from akunaki.domain.jobs import EnqueuedJob, JobRole

_CTX = ToolContext(tenant_id="tenant-1", user_id="user-1")


@dataclass(frozen=True, slots=True)
class _Linked:
    """The connection rows the fakes serve, in both shapes the tools need."""

    connection_id: str
    provider: str
    tenant_id: str = "tenant-1"
    status: ConnectionStatus = ConnectionStatus.ACTIVE


class _FakeConnections:
    """Backs both tools: the listing source and the sync lookup."""

    def __init__(self, rows: list[_Linked]) -> None:
        self._rows = rows

    def connection_statuses(self, *, tenant_id: str) -> list[ConnectionSummary]:
        return [
            ConnectionSummary(
                connection_id=row.connection_id,
                provider=row.provider,
                status=row.status.value,
                last_success_at=None,
                last_error_class=None,
                consecutive_failures=0,
                transport_pages=0,
                raw_revisions=0,
            )
            for row in self._rows
            if row.tenant_id == tenant_id
        ]

    def get_connection(self, *, connection_id: str) -> LinkedConnection | None:
        for row in self._rows:
            if row.connection_id == connection_id:
                return LinkedConnection(
                    connection_id=row.connection_id,
                    tenant_id=row.tenant_id,
                    provider=Provider(row.provider),
                    status=row.status,
                    scopes=(),
                    external_user_id=None,
                )
        return None


class _FakeJobs:
    """Records enqueues so a test can assert what the sync actually queued."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str]] = []

    def enqueue_job(
        self,
        *,
        job_id: str,
        tenant_id: str,
        job_type: str,
        payload_json: str,
        now: datetime,
        role: JobRole = JobRole.CORE,
        priority: int = 100,
        run_after: datetime | None = None,
        max_attempts: int = 5,
        idempotency_key: str | None = None,
    ) -> EnqueuedJob:
        self.enqueued.append((job_type, payload_json))
        return EnqueuedJob(
            job_id=job_id,
            tenant_id=tenant_id,
            job_type=job_type,
            role=role,
            created=True,
        )


def _registry(rows: list[_Linked], jobs: _FakeJobs) -> ToolRegistry:
    connections = _FakeConnections(rows)
    registry = ToolRegistry()
    register_connection_tools(
        registry,
        connections=ConnectionsSurfaceService(connections=connections),
        sync=SyncRequestService(
            connections=connections,
            jobs=jobs,
            new_id=lambda: "job-1",
        ),
    )
    return registry


def test_a_listed_connection_can_be_synced_through_the_registry() -> None:
    """The whole point: list, then sync what was listed, by name only.

    This is the test that would have caught ``connection_id`` missing from the
    listing — every per-tool test passed while the pair was unusable together.
    """
    jobs = _FakeJobs()
    registry = _registry([_Linked(connection_id="conn-1", provider="polar")], jobs)

    listed = registry.get("connections.list").invoke({}, _CTX).model_dump()
    # The caller knows nothing but what the listing told it.
    [connection] = listed["connections"]
    synced = (
        registry.get("connections.sync")
        .invoke({"connection_id": connection["connection_id"]}, _CTX)
        .model_dump()
    )

    assert synced["created"] is True
    assert synced["job_id"] == "job-1"
    [(job_type, payload)] = jobs.enqueued
    assert job_type == "connection.incremental_sync"
    assert "conn-1" in payload


def test_the_listing_supplies_every_argument_the_sync_tool_requires() -> None:
    """Pins the contract, not just one happy path.

    Asserting that the listing's fields *cover* the sync tool's required input
    means a future required argument the listing cannot supply breaks this test
    rather than silently stranding the tool again.
    """
    registry = _registry([_Linked(connection_id="conn-1", provider="polar")], _FakeJobs())

    listed = registry.get("connections.list").invoke({}, _CTX).model_dump()
    sync_input = registry.get("connections.sync").input_model
    required = {name for name, field in sync_input.model_fields.items() if field.is_required()}

    assert required, "the sync tool takes arguments; otherwise this test proves nothing"
    assert required <= set(listed["connections"][0])


def test_each_listed_connection_is_individually_syncable() -> None:
    """A tenant with several connectors must be able to refresh any of them."""
    jobs = _FakeJobs()
    registry = _registry(
        [
            _Linked(connection_id="conn-oura", provider="oura"),
            _Linked(connection_id="conn-polar", provider="polar"),
        ],
        jobs,
    )

    listed = registry.get("connections.list").invoke({}, _CTX).model_dump()
    for connection in listed["connections"]:
        registry.get("connections.sync").invoke(
            {"connection_id": connection["connection_id"]}, _CTX
        )

    assert len(jobs.enqueued) == 2
    payloads = "".join(payload for _job_type, payload in jobs.enqueued)
    assert "conn-oura" in payloads
    assert "conn-polar" in payloads


def test_the_listing_never_reveals_another_tenants_connection() -> None:
    """The id is safe to publish *because* the tenant scope is real.

    Exposing an identifier is only reasonable if it cannot be used to reach
    something the caller does not own, so the chain test has to prove the
    boundary it depends on.
    """
    jobs = _FakeJobs()
    registry = _registry(
        [
            _Linked(connection_id="conn-mine", provider="polar"),
            _Linked(connection_id="conn-theirs", provider="oura", tenant_id="tenant-2"),
        ],
        jobs,
    )

    listed = registry.get("connections.list").invoke({}, _CTX).model_dump()

    assert [c["connection_id"] for c in listed["connections"]] == ["conn-mine"]
    # And naming the unlisted id directly is refused, so the listing is not the
    # only thing standing between a caller and another tenant's connection.
    with pytest.raises(LookupError):
        registry.get("connections.sync").invoke({"connection_id": "conn-theirs"}, _CTX)
    assert jobs.enqueued == []
