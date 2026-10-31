import json
from datetime import datetime
from uuid import uuid4

import pytest

from core.llm import LLMError
from core.llm.fake import FakeProvider
from core.models import Agent
from core.planner import (
    MAX_TASKS,
    PlanningError,
    PlanValidationError,
    build_system_prompt,
    parse_plan,
    plan_mission,
)

AGENT_NAMES = {"researcher", "coder"}


def make_agent(name: str, description: str = "does things") -> Agent:
    return Agent(
        id=uuid4(),
        name=name,
        description=description,
        capabilities=["web.search"],
        created_at=datetime(2026, 1, 1),
    )


def task(key, agent="researcher", instruction="do it", depends_on=()):
    return {
        "key": key,
        "agent": agent,
        "instruction": instruction,
        "depends_on": list(depends_on),
    }


def plan_json(*tasks) -> str:
    return json.dumps({"tasks": list(tasks)})


# ---------- parse_plan ----------

def test_valid_plan_is_returned_in_dependency_order():
    text = plan_json(
        task("t2", depends_on=["t1"]),
        task("t1"),
        task("t3", agent="coder", depends_on=["t2"]),
    )
    plan = parse_plan(text, AGENT_NAMES)
    assert [t.key for t in plan.tasks] == ["t1", "t2", "t3"]


def test_independent_tasks_have_no_forced_order():
    plan = parse_plan(plan_json(task("a"), task("b")), AGENT_NAMES)
    assert [t.key for t in plan.tasks] == ["a", "b"]


def test_json_inside_code_fence_is_accepted():
    text = "```json\n" + plan_json(task("t1")) + "\n```"
    assert [t.key for t in parse_plan(text, AGENT_NAMES).tasks] == ["t1"]


def test_json_surrounded_by_prose_is_accepted():
    text = "Sure! Here is the plan: " + plan_json(task("t1")) + " Hope that helps."
    assert [t.key for t in parse_plan(text, AGENT_NAMES).tasks] == ["t1"]


def test_whitespace_is_stripped():
    plan = parse_plan(plan_json(task("t1", instruction="  do it  ")), AGENT_NAMES)
    assert plan.tasks[0].instruction == "do it"


@pytest.mark.parametrize(
    "text, expected_fragment",
    [
        ("not json at all", "not valid JSON"),
        ("[]", "schema errors"),
        (json.dumps({"tasks": []}), "schema errors"),
        (json.dumps({"nope": 1}), "schema errors"),
        (plan_json({"key": "t1", "agent": "researcher"}), "instruction"),
        (plan_json(task("bad key!")), "schema errors"),
        (plan_json(task("t1", instruction="   ")), "schema errors"),
        (plan_json(task("t1"), task("t1")), "duplicate task keys: t1"),
        (plan_json(task("t1", agent="wizard")), "unknown agent 'wizard'"),
        (plan_json(task("t1", depends_on=["t1"])), "depends on itself"),
        (plan_json(task("t1", depends_on=["ghost"])), "unknown task(s): ghost"),
        (
            plan_json(task("t1", depends_on=["t2"]), task("t2", depends_on=["t1"])),
            "cycle",
        ),
        (plan_json(*[task(f"t{i}") for i in range(MAX_TASKS + 1)]), "schema errors"),
    ],
)
def test_invalid_plans_are_rejected(text, expected_fragment):
    with pytest.raises(PlanValidationError) as exc:
        parse_plan(text, AGENT_NAMES)
    assert expected_fragment in str(exc.value)


def test_max_tasks_is_allowed():
    text = plan_json(*[task(f"t{i}") for i in range(MAX_TASKS)])
    assert len(parse_plan(text, AGENT_NAMES).tasks) == MAX_TASKS


# ---------- prompt ----------

def test_system_prompt_lists_agents_and_rules():
    prompt = build_system_prompt(
        [make_agent("researcher", "finds facts"), make_agent("coder")]
    )
    assert "researcher: finds facts" in prompt
    assert "coder" in prompt
    assert "web.search" in prompt
    assert "depends_on" in prompt


# ---------- plan_mission ----------

AGENTS = [make_agent("researcher"), make_agent("coder")]


def test_plan_mission_success_first_try():
    fake = FakeProvider([plan_json(task("t1"), task("t2", depends_on=["t1"]))])
    plan = plan_mission("compare databases", AGENTS, provider=fake)

    assert [t.key for t in plan.tasks] == ["t1", "t2"]
    assert len(fake.calls) == 1
    assert fake.calls[0]["json_mode"] is True
    assert "researcher" in fake.calls[0]["system"]
    assert "compare databases" in fake.calls[0]["prompt"]


def test_invalid_output_triggers_one_repair_with_the_error_shown():
    bad = plan_json(task("t1", agent="wizard"))
    good = plan_json(task("t1"))
    fake = FakeProvider([bad, good])

    plan = plan_mission("objective", AGENTS, provider=fake)

    assert [t.key for t in plan.tasks] == ["t1"]
    assert len(fake.calls) == 2
    repair_prompt = fake.calls[1]["prompt"]
    assert "unknown agent 'wizard'" in repair_prompt
    assert "wizard" in repair_prompt  # the bad answer itself is included
    assert "objective" in repair_prompt


def test_gives_up_after_repair_attempts_are_used():
    fake = FakeProvider(["garbage", "still garbage"])
    with pytest.raises(PlanningError) as exc:
        plan_mission("objective", AGENTS, provider=fake)
    assert "after 2 attempts" in str(exc.value)
    assert len(fake.calls) == 2


def test_repairs_can_be_disabled():
    fake = FakeProvider(["garbage", plan_json(task("t1"))])
    with pytest.raises(PlanningError):
        plan_mission("objective", AGENTS, provider=fake, max_repairs=0)
    assert len(fake.calls) == 1


def test_llm_errors_are_not_retried_here():
    fake = FakeProvider([])  # raises LLMError on the first call
    with pytest.raises(LLMError):
        plan_mission("objective", AGENTS, provider=fake)
    assert len(fake.calls) == 1


def test_no_agents_registered():
    with pytest.raises(PlanningError):
        plan_mission("objective", [], provider=FakeProvider([]))