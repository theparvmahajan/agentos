"""A deterministic stand-in for an LLM agent: it sleeps for a fixed time.

Real agents take an unpredictable time and a single local GPU may serialize their calls, which
would hide any speedup from parallel workers. Sleeping tasks measure the orchestration itself:
scheduling, queueing, claiming and bookkeeping. No agent row named "sleeper" exists unless the
benchmark (or a test) creates one, so the planner never offers it.

Instruction format:  "sleep 0.5"  (seconds, at most MAX_SLEEP_SECONDS)
"""

import re
import time

from core.llm import LLMProvider
from core.models import Agent
from core.runner import AgentError, AgentResult, DependencyOutput
from core.tools import ToolRegistry

MAX_SLEEP_SECONDS = 30.0
_SLEEP = re.compile(r"^\s*sleep\s+(\d+(?:\.\d+)?)\s*$", re.IGNORECASE)


def parse_sleep(instruction: str) -> float:
    match = _SLEEP.match(instruction)
    if match is None:
        raise AgentError(f"sleeper instruction must look like 'sleep 0.5', got: {instruction[:60]!r}")
    seconds = float(match.group(1))
    if seconds > MAX_SLEEP_SECONDS:
        raise AgentError(f"sleep of {seconds}s is over the {MAX_SLEEP_SECONDS:g}s limit")
    return seconds


def run_sleeper(
    agent: Agent,
    instruction: str,
    dependencies: list[DependencyOutput],
    *,
    tools: ToolRegistry,
    provider: LLMProvider | None = None,
) -> AgentResult:
    seconds = parse_sleep(instruction)
    time.sleep(seconds)
    return AgentResult(output=f"slept {seconds:g}s after {len(dependencies)} dependencies")
