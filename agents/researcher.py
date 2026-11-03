from agents.loop import (
    DATA_RULE,
    JSON_ONLY_RULE,
    MAX_STEPS,
    FinalAction,
    Role,
    StepError,
    ToolAction,
    build_prompt,
    parse_step,
    run_agent_loop,
)
from agents.loop import build_system_prompt as _build_system_prompt
from core.llm import LLMProvider, TaskType
from core.models import Agent
from core.runner import AgentResult, DependencyOutput
from core.tools import ToolRegistry

__all__ = [
    "MAX_STEPS",
    "RESEARCHER",
    "FinalAction",
    "StepError",
    "ToolAction",
    "build_prompt",
    "build_system_prompt",
    "parse_step",
    "run_researcher",
]

RESEARCHER = Role(
    name="researcher",
    task_type=TaskType.RESEARCH,
    intro=(
        "You are the Researcher agent in a multi-agent runtime. "
        "You complete ONE task, then report your findings."
    ),
    rules=(
        (
            "Use a tool only when you need information you do not already have. "
            "Prefer one to three well-chosen searches over many."
        ),
        DATA_RULE,
        (
            "The final answer must be self-contained and based on what you found. "
            "Mention source URLs for important claims. If the tools returned nothing "
            "useful, or sources disagree, say so plainly instead of guessing."
        ),
        JSON_ONLY_RULE,
    ),
    answer_hint="<your complete findings>",
)


def build_system_prompt(tool_schemas: list[dict]) -> str:
    return _build_system_prompt(RESEARCHER, tool_schemas)


def run_researcher(
    agent: Agent,
    instruction: str,
    dependencies: list[DependencyOutput],
    *,
    tools: ToolRegistry,
    provider: LLMProvider | None = None,
) -> AgentResult:
    return run_agent_loop(
        RESEARCHER, agent, instruction, dependencies, tools=tools, provider=provider
    )