"""Privacy lifecycle tool: irreversible tenant erasure.

``privacy.delete`` is the registry's only ``confirmation=ALWAYS`` tool. Every
caller redeems a confirmation bound to the exact call — including a person in
their own session. A CSRF token proves the request came from our page; it does
not prove the human meant to erase everything they have.

The canonical registry lists this as ``enqueue_job``, which assumes an async
pipeline. Ours is **synchronous** — a handful of scoped deletes — so the tool
declares ``destroy_data`` instead: by the time the caller reads the response the
data really is gone, and "queued" would imply a cancellable window that does not
exist.

``model_exposure`` is **False**. A model may be told this capability exists, but
it must never be the thing that invokes irreversible erasure; the confirmation
already forbids acting without the user, and refusing model exposure means a
prompt-injected agent cannot even attempt the call.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from akunaki.application.deletion_service import DeletionService
from akunaki.application.tool_registry import (
    ConfirmationPolicy,
    Sensitivity,
    SideEffect,
    Tool,
    ToolContext,
    ToolRegistry,
)

DELETE_PRIVACY_SCOPE = "delete:privacy"

__all__ = [
    "DELETE_PRIVACY_SCOPE",
    "DeleteInput",
    "DeleteOutput",
    "delete_tenant_tool",
    "register_privacy_tools",
]


class DeleteInput(BaseModel):
    """No arguments: the tenant erased is always the caller's own.

    Taking a tenant id would make the tool's binding depend on a value the
    caller supplies, and there is no legitimate call that erases someone else.
    """


class DeleteOutput(BaseModel):
    """What the deletion erased. Counts only — no identity, no health values."""

    deletion_request_id: str = Field(
        description="Handle for the status read; outlives the tenant it erased.",
    )
    status: str
    rows_scrubbed: int
    jobs_cancelled: int


def delete_tenant_tool(service: DeletionService) -> Tool[DeleteInput, DeleteOutput]:
    """The ``privacy.delete`` tool: erase the caller's tenant, irreversibly."""

    def handler(inputs: DeleteInput, context: ToolContext) -> DeleteOutput:
        outcome = service.delete_tenant(
            tenant_id=context.tenant_id,
            now=datetime.now(UTC),
        )
        return DeleteOutput(
            deletion_request_id=outcome.request_id,
            status=outcome.status.value,
            rows_scrubbed=outcome.counts.total_rows,
            jobs_cancelled=outcome.counts.jobs_cancelled,
        )

    return Tool(
        name="privacy.delete",
        input_model=DeleteInput,
        output_model=DeleteOutput,
        handler=handler,
        scopes=(DELETE_PRIVACY_SCOPE,),
        sensitivity=Sensitivity.DESTRUCTIVE,
        # Not enqueue_job: the erasure completes before this returns, so a
        # caller is never told to expect a cancellable window that does not exist.
        side_effect=SideEffect.DESTROY_DATA,
        model_exposure=False,
        confirmation=ConfirmationPolicy.ALWAYS,
        audit="privacy.delete",
    )


def register_privacy_tools(registry: ToolRegistry, *, deletion: DeletionService) -> None:
    """Register the privacy lifecycle tools on a registry."""
    registry.register(delete_tenant_tool(deletion))
