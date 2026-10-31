from dataclasses import dataclass
from typing import Protocol

from core.llm import LLMProvider
from core.models import Agent
from core.tools import ToolRegistry


class AgentError(Exception):
    """An agent could not complete its task."""


@dataclass(frozen=True)
class DependencyOutput:
    """The finished output of a task this task depends on."""

    key: str
    instruction: str
    output: str


@dataclass(frozen=True)
class AgentResult:
    output: str
    steps: int = 1
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    llm_latency_ms: int = 0


class Runner(Protocol):
    """Anything that can do a task for an agent. The executor only knows this shape."""

    def __call__(
        self,
        agent: Agent,
        instruction: str,
        dependencies: list[DependencyOutput],
        *,
        tools: ToolRegistry,
        provider: LLMProvider | None = None,
    ) -> AgentResult: ...