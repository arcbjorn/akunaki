"""Connection lifecycle tools: the read subset.

``connections.list`` is the read half of the canonical registry's lifecycle
group. It answers *is my data actually flowing?* — which providers are linked,
whether each is healthy, and how much has been ingested.

The two tools are meant to **chain**: ``list`` reports each connection's
``connection_id``, which is exactly the argument ``sync`` takes. That is the
same list-then-act pairing ``health.get_recent_workouts`` and
``health.get_workout`` form, and it is why the id belongs in the listing —
without it the mutating tool is unreachable from the registry it lives in.

Scoped to ``read:connections``, not ``read:health``: connection metadata is not
health data, and folding it into the health scope would over-grant a caller
that only needs to read a day view.

``connections.sync`` is the registry's first **mutating** tool. It enqueues the
same incremental-sync job webhooks and the reconcile sweep use, so an agent
cannot reach a sync path a user could not. It declares
``requires_confirmation``, which the invoke route enforces for calls carrying a
``run_id`` — the canonical registry's "yes if agent". A person pressing "sync
now" in their own session performs an explicit, CSRF-enforced act and needs no
second approval; a call claiming to come from an agent run must redeem a
confirmation bound to that exact call.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from akunaki.application.connections_surface import ConnectionsSurfaceService
from akunaki.application.sync_request import SyncRequestRejection, SyncRequestService
from akunaki.application.tool_registry import (
    ConfirmationPolicy,
    Sensitivity,
    SideEffect,
    Tool,
    ToolContext,
    ToolRegistry,
)

READ_CONNECTIONS_SCOPE = "read:connections"
# Enqueuing a sync is a write against the connection, not a health read.
WRITE_CONNECTIONS_SCOPE = "write:connections"

__all__ = [
    "READ_CONNECTIONS_SCOPE",
    "WRITE_CONNECTIONS_SCOPE",
    "ConnectionDTO",
    "ConnectionsInput",
    "ConnectionsOutput",
    "list_connections_tool",
    "register_connection_tools",
    "sync_connection_tool",
]


class ConnectionsInput(BaseModel):
    """No inputs: the tenant comes from the tool context, never the caller."""


class ConnectionDTO(BaseModel):
    """One linked connection's status and ingest progress.

    Carries no health values. ``last_error_class`` is an error *class* only, so
    a failing connector cannot leak a vendor body into a model's context.

    ``connection_id`` is what makes the lifecycle group usable as a group: it is
    the argument ``connections.sync`` takes, so without it a caller that listed
    its connections had no way to name one to refresh. It is an opaque
    per-tenant uuid — not health data, and not a vendor identifier — and
    ``sync`` re-checks tenant ownership on every call, so holding one grants
    nothing on its own.
    """

    connection_id: str
    provider: str
    status: str = Field(description="pending, active, needs_reauth, revoked, or error.")
    last_success_at: str | None
    last_error_class: str | None
    consecutive_failures: int
    transport_pages: int
    raw_revisions: int


class ConnectionsOutput(BaseModel):
    """The caller's connections. Empty when nothing is linked."""

    connections: list[ConnectionDTO]


def list_connections_tool(
    service: ConnectionsSurfaceService,
) -> Tool[ConnectionsInput, ConnectionsOutput]:
    """The ``connections.list`` tool over the connections surface."""

    def handler(inputs: ConnectionsInput, context: ToolContext) -> ConnectionsOutput:
        summaries = service.connections_for_tenant(tenant_id=context.tenant_id)
        return ConnectionsOutput(
            connections=[
                ConnectionDTO(
                    connection_id=summary.connection_id,
                    provider=summary.provider,
                    status=summary.status,
                    last_success_at=summary.last_success_at,
                    last_error_class=summary.last_error_class,
                    consecutive_failures=summary.consecutive_failures,
                    transport_pages=summary.transport_pages,
                    raw_revisions=summary.raw_revisions,
                )
                for summary in summaries
            ]
        )

    return Tool(
        name="connections.list",
        input_model=ConnectionsInput,
        output_model=ConnectionsOutput,
        handler=handler,
        scopes=(READ_CONNECTIONS_SCOPE,),
        # Not health data: statuses, timestamps, and counts only.
        sensitivity=Sensitivity.LOW,
        model_exposure=True,
        audit="connections.list",
    )


class SyncInput(BaseModel):
    """Which connection to refetch now."""

    connection_id: str = Field(min_length=1)


class SyncOutput(BaseModel):
    """The queued sync."""

    job_id: str
    created: bool = Field(
        description="False when an identical sync was already in flight (deduped).",
    )


def sync_connection_tool(service: SyncRequestService) -> Tool[SyncInput, SyncOutput]:
    """The ``connections.sync`` tool: enqueue a refetch for one connection.

    The first **mutating** tool in the registry. It enqueues a job rather than
    writing health data, and the job it queues is the same one webhooks and the
    reconcile sweep use — so an agent cannot reach a sync path a user could not.

    ``requires_confirmation`` is set, which the invoke route enforces only for
    calls carrying a ``run_id`` — the design's "yes if agent". A person pressing
    "sync now" in their own session is already an explicit act; a call claiming
    to originate in an agent run must prove the user authorized *that* call.
    """

    def handler(inputs: SyncInput, context: ToolContext) -> SyncOutput:
        outcome = service.request_sync(
            tenant_id=context.tenant_id,
            connection_id=inputs.connection_id,
            # The tool's idempotency is the confirmation's: one authorized call
            # enqueues one job. Without a run there is no confirmation, so the
            # connection id alone collapses repeats while one is in flight.
            idempotency_key=context.user_id,
            now=datetime.now(UTC),
        )
        if outcome is SyncRequestRejection.NOT_FOUND:
            msg = "connection not found"
            raise LookupError(msg)
        if outcome is SyncRequestRejection.NOT_SYNCABLE:
            msg = "connection cannot sync until it is re-linked"
            raise ValueError(msg)
        return SyncOutput(job_id=outcome.job_id, created=outcome.created)

    return Tool(
        name="connections.sync",
        input_model=SyncInput,
        output_model=SyncOutput,
        handler=handler,
        scopes=(WRITE_CONNECTIONS_SCOPE,),
        sensitivity=Sensitivity.LOW,
        side_effect=SideEffect.ENQUEUE_JOB,
        # A model may ask for a sync, but only against a confirmation the user
        # granted out-of-band for that exact call.
        model_exposure=True,
        confirmation=ConfirmationPolicy.IF_AGENT,
        audit="connections.sync",
    )


def register_connection_tools(
    registry: ToolRegistry,
    *,
    connections: ConnectionsSurfaceService,
    sync: SyncRequestService | None = None,
) -> None:
    """Register the connection tools on a registry."""
    registry.register(list_connections_tool(connections))
    if sync is not None:
        registry.register(sync_connection_tool(sync))
