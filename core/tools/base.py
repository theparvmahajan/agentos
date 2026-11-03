from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


class ToolError(Exception):
    """A tool call failed. The message is written so an agent (or a person) can act on it."""


class ToolUnavailable(ToolError):
    """The tool cannot work at all right now (e.g. Docker is not running). Trying again
    with different arguments is pointless, so the agent stops instead of burning LLM calls."""


class UnknownTool(ToolError):
    pass


class ToolPermissionError(ToolError):
    pass


class ToolInputError(ToolError):
    pass


@dataclass(frozen=True)
class ToolResult:
    tool: str
    output: str
    latency_ms: int


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    capability: str  # the capability an agent must hold to use this tool
    input_model: type[BaseModel]
    handler: Callable[[Any], str]

    def schema(self) -> dict:
        """What an LLM needs to know to call this tool."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.input_model.model_json_schema(),
        }