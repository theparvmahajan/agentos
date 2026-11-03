from agents.loop import DATA_RULE, JSON_ONLY_RULE, Role, run_agent_loop
from core.llm import LLMProvider, TaskType
from core.models import Agent
from core.runner import AgentResult, DependencyOutput
from core.tools import ToolRegistry


def _must_have_run_code(answer: str, tool_successes: int) -> str | None:
    """A deterministic guard: the Coder may not report results it never produced."""
    if tool_successes == 0:
        return "you have not successfully run any code yet; call run_python first"
    return None


CODER = Role(
    name="coder",
    task_type=TaskType.CODING,
    intro=(
        "You are the Coder agent in a multi-agent runtime. You complete ONE task by writing "
        "and running a small Python program, then you report the code and what it really printed."
    ),
    rules=(
        ("Write small, self-contained Python 3 programs that use only the standard library. "
        "There is no internet and there are no extra packages."),
        ("ALWAYS run your code with run_python before your final answer, and read the output. "
        "If it fails or prints something wrong, fix the code and run it again."),
        ("Print every result you want to report. Never state an output you did not see "
        "in a tool result."),
        DATA_RULE,
        ("The final answer must contain the final code in a ```python block, then the exact "
        "output of its last run, then one line saying whether it worked. If it never worked, "
        "say so plainly instead of pretending."),
        "Inside the JSON, write code as a string and escape newlines as \\n and quotes as \\\".",
        JSON_ONLY_RULE,
    ),
    answer_hint="<final code, its verified output, and whether it worked>",
    max_steps=8,
    check_answer=_must_have_run_code,
)


def run_coder(
    agent: Agent,
    instruction: str,
    dependencies: list[DependencyOutput],
    *,
    tools: ToolRegistry,
    provider: LLMProvider | None = None,
) -> AgentResult:
    return run_agent_loop(CODER, agent, instruction, dependencies, tools=tools, provider=provider)
