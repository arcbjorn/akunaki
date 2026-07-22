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
from typing import Generic, TypeVar

from pydantic import BaseModel

_In = TypeVar("_In", bound=BaseModel)
_Out = TypeVar("_Out", bound=BaseModel)


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


@dataclass(frozen=True, slots=True)
class ToolContext:
    """The caller identity a tool executes under.

    The tenant is always the authenticated tenant, never a tool argument — a
    tool can no more read another tenant's data than a REST handler can.
    """

    tenant_id: str
    user_id: str


@dataclass(frozen=True, slots=True)
class Tool(Generic[_In, _Out]):
    """A typed capability plus its metadata.

    ``handler`` receives the validated input and the caller context and returns
    the output model. Metadata is declarative so adapters (REST/MCP/agent) can
    enforce scopes, sensitivity, and confirmation uniformly.
    """

    name: str
    input_model: type[_In]
    output_model: type[_Out]
    handler: Callable[[_In, ToolContext], _Out]
    version: str = "v0.1.0"
    scopes: tuple[str, ...] = ()
    sensitivity: Sensitivity = Sensitivity.LOW
    side_effect: SideEffect = SideEffect.NONE
    model_exposure: bool = False
    requires_confirmation: bool = False
    audit: str | None = None

    def invoke(self, raw_input: dict[str, object], context: ToolContext) -> _Out:
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

    def register(self, tool: Tool[_In, _Out]) -> None:
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
