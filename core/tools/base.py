from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

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


class ToolDenied(ToolPermissionError):
    """The policy refused this call. Never retried: running it again cannot change the answer."""

    def __init__(self, message: str, *, rule: str = ""):
        super().__init__(message)
        self.rule = rule


class ApprovalRequired(ToolDenied):
    """The policy wants a human to approve this call. Until milestone M6 (human-in-the-loop)
    exists there is nobody to ask, so the call is refused. It fails closed, never open."""


class ToolInputError(ToolError):
    pass


@dataclass(frozen=True)
class ToolContext:
    """Who a tool call is for. The worker binds one per task attempt (ToolRegistry.bind), so the
    audit trail can say which task and mission a call belonged to, without the agent code (or the
    model) ever being able to choose or change it."""

    task_id: UUID
    mission_id: UUID
    tenant_id: UUID | None = None
    attempt: int | None = None
    tainted: bool = False  # the task already depends on untrusted content


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