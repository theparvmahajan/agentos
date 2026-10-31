import time

from pydantic import ValidationError

from core.models import Agent
from core.tools.base import (
    Tool,
    ToolError,
    ToolInputError,
    ToolPermissionError,
    ToolResult,
    UnknownTool,
)

MAX_OUTPUT_CHARS = 8000


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n[output truncated: {len(text)} characters in total]"


class ToolRegistry:
    """The only way an agent reaches a tool.

    Every call passes the same gate: tool exists -> agent holds the capability ->
    arguments validate -> handler runs -> output is size-limited.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def tools_for(self, agent: Agent) -> list[Tool]:
        return [t for t in self._tools.values() if t.capability in agent.capabilities]

    def describe_for(self, agent: Agent) -> list[dict]:
        return [t.schema() for t in self.tools_for(agent)]

    def call(self, agent: Agent, tool_name: str, raw_args: dict) -> ToolResult:
        tool = self._tools.get(tool_name)
        if tool is None:
            # Only list tools this agent may use, so the error doesn't leak the rest.
            allowed = ", ".join(t.name for t in self.tools_for(agent)) or "none"
            raise UnknownTool(f"unknown tool '{tool_name}'; available tools: {allowed}")

        if tool.capability not in agent.capabilities:
            raise ToolPermissionError(
                f"agent '{agent.name}' lacks capability '{tool.capability}' "
                f"required by tool '{tool.name}'"
            )

        try:
            args = tool.input_model.model_validate(raw_args)
        except ValidationError as e:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
            )
            raise ToolInputError(f"invalid arguments for {tool.name}: {problems}") from e

        start = time.perf_counter()
        try:
            output = tool.handler(args)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"tool {tool.name} crashed: {e!r}") from e
        latency_ms = int((time.perf_counter() - start) * 1000)

        return ToolResult(tool=tool.name, output=_truncate(output), latency_ms=latency_ms)