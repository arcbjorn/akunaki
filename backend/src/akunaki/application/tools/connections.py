"""Connection lifecycle tools: the read subset.

``connections.list`` is the read half of the canonical registry's lifecycle
group. It answers *is my data actually flowing?* — which providers are linked,
whether each is healthy, and how much has been ingested.

Scoped to ``read:connections``, not ``read:health``: connection metadata is not
health data, and folding it into the health scope would over-grant a caller
that only needs to read a day view.

``connections.sync`` (``enqueue_job``, confirmation required from an agent) is
deliberately **not** here. It is a mutation, and the confirmation machinery the
design requires for agent-invoked mutations — one-time, expiring, bound to a
canonical args hash — does not exist yet. Registering it without that would
make a mutating tool model-invocable on the honour system.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from akunaki.application.connections_surface import ConnectionsSurfaceService
from akunaki.application.tool_registry import (
    Sensitivity,
    Tool,
    ToolContext,
    ToolRegistry,
)

READ_CONNECTIONS_SCOPE = "read:connections"

__all__ = [
    "READ_CONNECTIONS_SCOPE",
    "ConnectionDTO",
    "ConnectionsInput",
    "ConnectionsOutput",
    "list_connections_tool",
    "register_connection_tools",
]


class ConnectionsInput(BaseModel):
    """No inputs: the tenant comes from the tool context, never the caller."""


class ConnectionDTO(BaseModel):
    """One linked connection's status and ingest progress.

    Carries no health values. ``last_error_class`` is an error *class* only, so
    a failing connector cannot leak a vendor body into a model's context.
    """

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


def register_connection_tools(
    registry: ToolRegistry,
    *,
    connections: ConnectionsSurfaceService,
) -> None:
    """Register the read-connection tools on a registry."""
    registry.register(list_connections_tool(connections))
