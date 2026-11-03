import json
from datetime import datetime
from uuid import uuid4

import pytest

from agents.loop import Role, build_system_prompt, run_agent_loop
from core.llm import TaskType
from core.llm.fake import FakeProvider
from core.llm.gateway import MODEL_OVERRIDES
from core.models import Agent
from core.runner import AgentError
from core.tools import ToolRegistry, build_default_registry
from core.tools.search import FakeSearchProvider


def make_agent(*capabilities: str) -> Agent:
    return Agent(
        id=uuid4(), name="a", capabilities=list(capabilities), created_at=datetime(2026, 1, 1)
    )


def final(answer: str) -> str:
    return json.dumps({"action": "final", "answer": answer})


def search_step() -> str:
    return json.dumps({"action": "tool", "tool": "web_search", "args": {"query": "q"}})


def role(**overrides) -> Role:
    fields = {
        "name": "tester",
        "task_type": TaskType.GENERAL,
        "intro": "You are the Tester agent.",
        "rules": ("Be brief.", "Output JSON."),
        "answer_hint": "<the verdict>",
    }
    return Role(**{**fields, **overrides})


def go(r, llm, *, capabilities=("web.search",), registry=None):
    registry = registry or build_default_registry(FakeSearchProvider([]))
    return run_agent_loop(r, make_agent(*capabilities), "do it", [], tools=registry, provider=llm)


# ---------- the prompt is built from the role ----------

def test_the_system_prompt_contains_the_roles_intro_rules_and_answer_hint():
    prompt = build_system_prompt(role(), [])
    assert "You are the Tester agent." in prompt
    assert "- Be brief.\n- Output JSON." in prompt
    assert '"answer": "<the verdict>"' in prompt
    assert "You have no tools" in prompt


def test_tools_appear_only_when_the_agent_has_some():
    schemas = [{"name": "t", "description": "d", "parameters": {}}]
    assert '"name": "t"' in build_system_prompt(role(), schemas)
    assert "You have no tools" not in build_system_prompt(role(), schemas)


# ---------- each role uses its own task type (the model-router seam) ----------

@pytest.mark.parametrize("task_type", [TaskType.RESEARCH, TaskType.CODING, TaskType.REVIEW])
def test_the_role_decides_which_model_is_asked(monkeypatch, task_type):
    monkeypatch.setitem(MODEL_OVERRIDES, task_type, "special-model")
    llm = FakeProvider([final("ok")])
    go(role(task_type=task_type), llm)
    assert llm.calls[0]["model"] == "special-model"


# ---------- answer checks ----------

def test_a_rejected_answer_is_explained_to_the_model_which_can_then_recover():
    def check(answer, tool_successes):
        return None if "sources" in answer else "cite your sources"

    llm = FakeProvider([final("bare claim"), final("claim with sources")])
    result = go(role(check_answer=check), llm)

    assert result.output == "claim with sources"
    assert result.steps == 2
    assert "your final answer was rejected (cite your sources)" in llm.calls[1]["prompt"]


def test_a_model_that_never_satisfies_the_check_fails_with_the_reason():
    llm = FakeProvider([final("nope")] * 3)
    with pytest.raises(AgentError) as exc:
        go(role(max_steps=3, check_answer=lambda a, n: "needs more"), llm)

    assert "no final answer after 3 steps" in str(exc.value)
    assert "needs more" in str(exc.value)
    assert len(llm.calls) == 3


def test_the_check_is_told_how_many_tool_calls_actually_succeeded():
    seen = []

    def check(answer, tool_successes):
        seen.append(tool_successes)

    bad = json.dumps({"action": "tool", "tool": "web_search", "args": {"query": ""}})
    llm = FakeProvider([search_step(), bad, final("done")])
    result = go(role(check_answer=check), llm)

    assert seen == [1]  # one call worked; the invalid one (empty query) does not count
    assert result.tool_calls == 2  # but both were attempted


# ---------- step limit is per role ----------

def test_max_steps_comes_from_the_role():
    llm = FakeProvider([search_step()] * 3)
    with pytest.raises(AgentError) as exc:
        go(role(max_steps=3), llm)
    assert "after 3 steps" in str(exc.value)
    assert len(llm.calls) == 3
    assert "last step" in llm.calls[2]["prompt"]


def test_a_role_with_no_tools_in_the_registry_still_answers():
    llm = FakeProvider([final("fine")])
    assert go(role(), llm, registry=ToolRegistry()).output == "fine"


# ---------- a tool that cannot work at all ends the task at once ----------

def test_an_unavailable_tool_fails_the_task_immediately_instead_of_burning_llm_calls():
    from pydantic import BaseModel

    from core.tools import Tool, ToolUnavailable

    class Nothing(BaseModel):
        pass

    def broken(_args):
        raise ToolUnavailable("Docker is not running")

    registry = ToolRegistry()
    registry.register(Tool("sandbox", "d", "code.execute", Nothing, broken))
    call = json.dumps({"action": "tool", "tool": "sandbox", "args": {}})
    llm = FakeProvider([call, final("never reached")])

    with pytest.raises(AgentError) as exc:
        go(role(), llm, capabilities=("code.execute",), registry=registry)

    assert "Docker is not running" in str(exc.value)
    assert len(llm.calls) == 1
