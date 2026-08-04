"""Typed capability facade over application services (phase two, AI-independent).

A tool is a stable, typed capability with Pydantic input/output models and the
metadata the design requires (scopes, sensitivity, side effect, idempotency,
model exposure, confirmation). The registry is the single facade every adapter
reuses — REST handlers, scheduled reports, an MCP adapter, or an agent tool
runner — so business rules and authorization live in one place, never per
channel.

This module is **independent of any model/AI package**: it imports no SDK and a
tool is a plain callable over application services. That is the phase-two exit
criterion "tools usable by REST without model packages".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel


class Sensitivity(StrEnum):
    """How sensitive a tool's data or action is."""

    LOW = "low"
    HEALTH_READ = "health_read"
    HEALTH_EXPORT = "health_export"
    DESTRUCTIVE = "destructive"


class SideEffect(StrEnum):
    """What a tool does beyond returning a value."""

    NONE = "none"
    ENQUEUE_JOB = "enqueue_job"
    MUTATE_PREFS = "mutate_prefs"
    EXTERNAL_CALL = "external_call"

    DESTROY_DATA = "destroy_data"
    """Irreversibly removes stored data before returning.

    Distinct from ``enqueue_job``: a caller told "queued" may reasonably expect
    a window to cancel, and there is none. The canonical registry lists
    ``privacy.delete`` as ``enqueue_job`` on the assumption of an async
    pipeline; ours completes inline, so saying ``enqueue_job`` would misreport
    what already happened.
    """


class ConfirmationPolicy(StrEnum):
    """When a tool needs an out-of-band confirmation before it may execute.

    The canonical registry states three distinct answers, so this is an enum
    rather than a bool: collapsing them would make "yes always" unexpressible
    and silently downgrade a destructive tool to the agent-only rule.
    """

    NEVER = "never"
    """Reads. The session's own authorization is the whole check."""

    IF_AGENT = "if_agent"
    """Mutations a person may perform directly.

    A direct session call is already an explicit, CSRF-enforced human act; a
    call carrying a ``run_id`` originates in an agent run and must prove the
    user authorized *that* call.
    """

    ALWAYS = "always"
    """Destructive or irreversible actions.

    Confirmed even for a direct session call: a CSRF token proves the request
    came from our page, not that the human meant to erase everything.
    """


@dataclass(frozen=True, slots=True)
class ToolContext:
    """The caller identity a tool executes under.

    The tenant is always the authenticated tenant, never a tool argument — a
    tool can no more read another tenant's data than a REST handler can.
    """

    tenant_id: str
    user_id: str


@dataclass(frozen=True, slots=True)
class Tool[In: BaseModel, Out: BaseModel]:
    """A typed capability plus its metadata.

    ``handler`` receives the validated input and the caller context and returns
    the output model. Metadata is declarative so adapters (REST/MCP/agent) can
    enforce scopes, sensitivity, and confirmation uniformly.
    """

    name: str
    input_model: type[In]
    output_model: type[Out]
    handler: Callable[[In, ToolContext], Out]
    version: str = "v0.1.0"
    scopes: tuple[str, ...] = ()
    sensitivity: Sensitivity = Sensitivity.LOW
    side_effect: SideEffect = SideEffect.NONE
    model_exposure: bool = False
    """Whether a model may invoke this tool.

    **Declared, not yet enforced.** No agent caller exists — `/v1/tools` is
    reached by a session-authenticated human — so there is nothing to enforce it
    against today. The agent adapter must consult this before dispatching, and
    `confirmation` is what actually guards a mutation in the meantime.
    """

    confirmation: ConfirmationPolicy = ConfirmationPolicy.NEVER
    audit: str | None = None
    """Audit action name for this tool, or None to record nothing."""

    @property
    def is_audited(self) -> bool:
        """Whether invoking this tool appends an audit event.

        **Mutations only.** The threat audit answers here is confused deputy —
        "the model tricked a tool into doing something" — which is about actions
        taken, not data read. Auditing the seven read tools would also make the
        chain a write bottleneck on the hottest path: every append serializes on
        a tail read, and a dashboard polling ``health.get_today`` would add
        thousands of rows a day that no reviewer will ever read.

        A read tool still declares an ``audit`` name; it is used for log
        correlation, not for a durable event.
        """
        return self.audit is not None and self.side_effect is not SideEffect.NONE

    @property
    def requires_confirmation(self) -> bool:
        """Whether this tool ever needs a confirmation.

        Kept for the listing surface, which reports *that* a tool is gated.
        Deciding whether a **particular** call needs one is
        :meth:`needs_confirmation`, because the answer depends on the caller.
        """
        return self.confirmation is not ConfirmationPolicy.NEVER

    def needs_confirmation(self, *, in_agent_run: bool) -> bool:
        """Whether *this call* must redeem a confirmation before executing.

        Fails closed by construction: every policy except ``NEVER`` requires one
        for an agent call, and ``ALWAYS`` requires one regardless of caller.
        """
        if self.confirmation is ConfirmationPolicy.NEVER:
            return False
        if self.confirmation is ConfirmationPolicy.ALWAYS:
            return True
        return in_agent_run

    def invoke(self, raw_input: dict[str, object], context: ToolContext) -> Out:
        """Validate the raw input against the model and run the handler.

        Validation happens here so every adapter gets the same typed contract; a
        malformed argument raises before the handler ever runs.
        """
        validated = self.input_model.model_validate(raw_input)
        return self.handler(validated, context)


class ToolNotFoundError(KeyError):
    """No tool is registered under the requested name."""


@dataclass(slots=True)
class ToolRegistry:
    """The registry of typed tools, keyed by stable dotted name."""

    _tools: dict[str, Tool[BaseModel, BaseModel]] = field(default_factory=dict)

    def register[In: BaseModel, Out: BaseModel](self, tool: Tool[In, Out]) -> None:
        """Register a tool. A duplicate name is a wiring error, not a silent overwrite."""
        if tool.name in self._tools:
            msg = f"tool already registered: {tool.name}"
            raise ValueError(msg)
        self._tools[tool.name] = tool  # type: ignore[assignment]

    def get(self, name: str) -> Tool[BaseModel, BaseModel]:
        """Return the tool for a name, or raise :class:`ToolNotFoundError`."""
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(name) from exc

    def names(self) -> tuple[str, ...]:
        """All registered tool names, sorted."""
        return tuple(sorted(self._tools))

    def __contains__(self, name: object) -> bool:
        return name in self._tools
