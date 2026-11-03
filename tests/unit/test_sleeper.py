from datetime import datetime
from uuid import uuid4

import pytest

from agents import build_default_runners, run_sleeper
from agents.sleeper import MAX_SLEEP_SECONDS, parse_sleep
from core.models import Agent
from core.runner import AgentError, DependencyOutput
from core.tools import ToolRegistry


def agent() -> Agent:
    return Agent(id=uuid4(), name="sleeper", created_at=datetime(2026, 1, 1))


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("sleep 0.5", 0.5), ("sleep 2", 2.0), ("  SLEEP   0.25  ", 0.25), ("sleep 0", 0.0)],
)
def test_parse_sleep_reads_the_duration(text, seconds):
    assert parse_sleep(text) == seconds


@pytest.mark.parametrize(
    "text", ["", "sleep", "sleep -1", "sleep abc", "sleep 1 then do evil", "do 1", "sleep 1e3"]
)
def test_parse_sleep_rejects_anything_else(text):
    with pytest.raises(AgentError):
        parse_sleep(text)


def test_a_sleep_over_the_limit_is_refused():
    with pytest.raises(AgentError, match="limit"):
        parse_sleep(f"sleep {MAX_SLEEP_SECONDS + 1}")


def test_it_sleeps_and_reports(monkeypatch):
    slept = []
    monkeypatch.setattr("agents.sleeper.time.sleep", slept.append)
    deps = [DependencyOutput("t1", "x", "y")]

    result = run_sleeper(agent(), "sleep 0.5", deps, tools=ToolRegistry())

    assert slept == [0.5]
    assert result.output == "slept 0.5s after 1 dependencies"


def test_the_runner_is_registered_but_no_agent_row_is_seeded():
    from scripts.seed_agents import AGENTS

    assert build_default_runners()["sleeper"] is run_sleeper
    assert "sleeper" not in {a["name"] for a in AGENTS}  # the planner must never offer it
