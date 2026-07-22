"""Tests for the typed tool registry core (independent of AI)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from akunaki.application.tool_registry import (
    SideEffect,
    Tool,
    ToolContext,
    ToolNotFoundError,
    ToolRegistry,
)


class _Echo(BaseModel):
    value: int


def _echo_tool() -> Tool[_Echo, _Echo]:
    return Tool(
        name="test.echo",
        input_model=_Echo,
        output_model=_Echo,
        handler=lambda inp, ctx: _Echo(value=inp.value + 1),
    )


_CTX = ToolContext(tenant_id="tenant-1", user_id="user-1")


def test_register_and_get() -> None:
    registry = ToolRegistry()
    registry.register(_echo_tool())
    assert "test.echo" in registry
    assert registry.names() == ("test.echo",)
    assert registry.get("test.echo").name == "test.echo"


def test_duplicate_registration_raises() -> None:
    registry = ToolRegistry()
    registry.register(_echo_tool())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_echo_tool())


def test_unknown_tool_raises() -> None:
    with pytest.raises(ToolNotFoundError):
        ToolRegistry().get("nope")


def test_invoke_validates_and_runs() -> None:
    tool = _echo_tool()
    result = tool.invoke({"value": 41}, _CTX)
    assert result.value == 42


def test_invoke_rejects_malformed_input() -> None:
    tool = _echo_tool()
    with pytest.raises(ValueError, match="value"):
        tool.invoke({"value": "not-an-int"}, _CTX)


def test_default_metadata_is_safe() -> None:
    tool = _echo_tool()
    # Defaults deny model exposure and confirmation, and assert no side effect.
    assert tool.model_exposure is False
    assert tool.requires_confirmation is False
    assert tool.side_effect is SideEffect.NONE
