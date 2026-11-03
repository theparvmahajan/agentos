import re
from enum import Enum

from agents.loop import DATA_RULE, JSON_ONLY_RULE, Role, run_agent_loop
from core.llm import LLMProvider, TaskType
from core.models import Agent
from core.runner import AgentError, AgentResult, DependencyOutput
from core.tools import ToolRegistry


class Verdict(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


_VERDICT_LINE = re.compile(r"^[\s*`_#>-]*VERDICT\s*:\s*(APPROVE|REJECT)\b", re.IGNORECASE)


def parse_verdict(output: str) -> Verdict | None:
    """The verdict is the first non-empty line: `VERDICT: APPROVE` or `VERDICT: REJECT`."""
    for line in output.splitlines():
        if line.strip():
            match = _VERDICT_LINE.match(line)
            return Verdict(match.group(1).lower()) if match else None
    return None


def _must_state_a_verdict(answer: str, tool_successes: int) -> str | None:
    if parse_verdict(answer) is None:
        return "the first line must be exactly 'VERDICT: APPROVE' or 'VERDICT: REJECT'"
    return None


REVIEWER = Role(
    name="reviewer",
    task_type=TaskType.REVIEW,
    intro=(
        "You are the Reviewer agent in a multi-agent runtime. You complete ONE task by "
        "critically checking the work of earlier tasks (given as context) and giving a verdict."
    ),
    rules=(
        ("Check the work against what was asked: is it correct, complete, and supported? "
        "For code, check that the output shown is plausible for the code and that errors "
        "were not ignored. For research, check that claims are backed by the sources given."),
        ("You cannot run code or search the web. Judge only what is in front of you "
        "and do not invent facts."),
        DATA_RULE,
        ("Start your answer with a first line that is exactly VERDICT: APPROVE or "
        "VERDICT: REJECT. Then list concrete findings, one per line. "
        "REJECT only for real problems (wrong, missing, unsupported), not style preferences."),
        JSON_ONLY_RULE,
    ),
    answer_hint="VERDICT: APPROVE or REJECT on the first line, then your findings",
    check_answer=_must_state_a_verdict,
)


def run_reviewer(
    agent: Agent,
    instruction: str,
    dependencies: list[DependencyOutput],
    *,
    tools: ToolRegistry,
    provider: LLMProvider | None = None,
) -> AgentResult:
    """A rejection is a normal, successful review: the task completes and its output says
    REJECT. Only a reviewer that could not do its job (nothing to review, no verdict) fails."""
    if not dependencies:
        raise AgentError("reviewer has nothing to review: the task has no dependencies")
    return run_agent_loop(
        REVIEWER, agent, instruction, dependencies, tools=tools, provider=provider
    )
