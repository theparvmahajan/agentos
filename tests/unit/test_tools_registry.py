from datetime import datetime
from uuid import uuid4

import pytest
from pydantic import BaseModel, Field

from core.models import Agent
from core.tools import (
    MAX_OUTPUT_CHARS,
    Tool,
    ToolError,
    ToolInputError,
    ToolPermissionError,
    ToolRegistry,
    UnknownTool,
)


def make_agent(*capabilities: str, name: str = "agent-x") -> Agent:
    return Agent(
        id=uuid4(),
        name=name,
        capabilities=list(capabilities),
        created_at=datetime(2026, 1, 1),
    )


class EchoInput(BaseModel):
    text: str = Field(min_length=1, description="text to repeat")
    times: int = Field(default=1, ge=1, le=3)


def echo_tool(handler=None, *, name="echo", capability="echo.use") -> Tool:
    return Tool(
        name=name,
        description="Repeats text",
        capability=capability,
        input_model=EchoInput,
        handler=handler or (lambda a: a.text * a.times),
    )


def registry_with(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    for t in tools:
        registry.register(t)
    return registry


# ---------- happy path ----------

def test_successful_call_returns_output_and_latency():
    registry = registry_with(echo_tool())
    result = registry.call(make_agent("echo.use"), "echo", {"text": "hi", "times": 2})
    assert result.tool == "echo"
    assert result.output == "hihi"
    assert isinstance(result.latency_ms, int) and result.latency_ms >= 0


def test_argument_defaults_are_applied():
    registry = registry_with(echo_tool())
    assert registry.call(make_agent("echo.use"), "echo", {"text": "a"}).output == "a"


def test_duplicate_registration_is_rejected():
    with pytest.raises(ValueError):
        registry_with(echo_tool(), echo_tool())


def test_error_types_share_a_base_class():
    for cls in (UnknownTool, ToolPermissionError, ToolInputError):
        assert issubclass(cls, ToolError)


# ---------- the gate ----------

def test_unknown_tool_lists_only_tools_the_agent_may_use():
    registry = registry_with(
        echo_tool(), echo_tool(name="secret", capability="secret.use")
    )
    with pytest.raises(UnknownTool) as exc:
        registry.call(make_agent("echo.use"), "nope", {})
    assert "echo" in str(exc.value)
    assert "secret" not in str(exc.value)


def test_missing_capability_is_denied_and_handler_never_runs():
    calls = []
    registry = registry_with(echo_tool(handler=lambda a: calls.append(a) or "ran"))

    with pytest.raises(ToolPermissionError) as exc:
        registry.call(make_agent("something.else", name="mallory"), "echo", {"text": "x"})

    assert calls == []
    assert "mallory" in str(exc.value)
    assert "echo.use" in str(exc.value)


def test_permission_is_checked_before_arguments_are_validated():
    registry = registry_with(echo_tool())
    with pytest.raises(ToolPermissionError):
        registry.call(make_agent(), "echo", {"text": ""})  # invalid args AND no capability


@pytest.mark.parametrize(
    "bad_args",
    [{}, {"text": ""}, {"text": "x", "times": 0}, {"text": "x", "times": 9}, "not a dict"],
)
def test_invalid_arguments_are_rejected_before_the_handler_runs(bad_args):
    calls = []
    registry = registry_with(echo_tool(handler=lambda a: calls.append(a) or "ran"))

    with pytest.raises(ToolInputError) as exc:
        registry.call(make_agent("echo.use"), "echo", bad_args)

    assert calls == []
    assert "echo" in str(exc.value)


# ---------- handler failures ----------

def test_tool_errors_from_the_handler_pass_through_unchanged():
    def handler(args):
        raise ToolError("upstream is down")

    registry = registry_with(echo_tool(handler=handler))
    with pytest.raises(ToolError) as exc:
        registry.call(make_agent("echo.use"), "echo", {"text": "x"})
    assert type(exc.value) is ToolError
    assert str(exc.value) == "upstream is down"


def test_unexpected_exceptions_become_tool_errors_with_the_cause_kept():
    def handler(args):
        return 1 / 0

    registry = registry_with(echo_tool(handler=handler))
    with pytest.raises(ToolError) as exc:
        registry.call(make_agent("echo.use"), "echo", {"text": "x"})
    assert "crashed" in str(exc.value)
    assert isinstance(exc.value.__cause__, ZeroDivisionError)


# ---------- output limits ----------

def test_oversized_output_is_truncated_with_a_marker():
    big = "x" * (MAX_OUTPUT_CHARS + 500)
    registry = registry_with(echo_tool(handler=lambda a: big))
    out = registry.call(make_agent("echo.use"), "echo", {"text": "x"}).output
    assert out.startswith("x" * MAX_OUTPUT_CHARS)
    assert "truncated" in out
    assert len(out) < MAX_OUTPUT_CHARS + 100


def test_output_at_the_limit_is_untouched():
    exact = "y" * MAX_OUTPUT_CHARS
    registry = registry_with(echo_tool(handler=lambda a: exact))
    assert registry.call(make_agent("echo.use"), "echo", {"text": "x"}).output == exact


# ---------- discovery ----------

def test_tools_for_and_describe_for_only_include_permitted_tools():
    registry = registry_with(
        echo_tool(), echo_tool(name="secret", capability="secret.use")
    )
    agent = make_agent("echo.use")

    assert [t.name for t in registry.tools_for(agent)] == ["echo"]
    described = registry.describe_for(agent)
    assert [d["name"] for d in described] == ["echo"]
    assert described[0]["description"] == "Repeats text"
    assert "text" in described[0]["parameters"]["properties"]
    assert registry.describe_for(make_agent()) == []