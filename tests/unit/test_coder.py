import json
from datetime import datetime
from uuid import uuid4

import pytest

from agents import build_default_runners, run_coder
from agents.coder import CODER
from core.llm import TaskType
from core.llm.fake import FakeProvider
from core.models import Agent
from core.runner import AgentError, DependencyOutput
from core.tools import build_default_registry
from core.tools.python_exec import run_python_local
from core.tools.search import FakeSearchProvider


def make_agent(*capabilities: str) -> Agent:
    return Agent(
        id=uuid4(),
        name="coder",
        capabilities=list(capabilities),
        created_at=datetime(2026, 1, 1),
    )


def run_step(code: str, **extra) -> str:
    return json.dumps(
        {"action": "tool", "tool": "run_python", "args": {"code": code, **extra}}
    )


def final(answer: str) -> str:
    return json.dumps({"action": "final", "answer": answer})


def go(llm, *, capabilities=("code.execute",), dependencies=()):
    registry = build_default_registry(FakeSearchProvider([]), code_runner=run_python_local)
    return run_coder(
        make_agent(*capabilities), "compute it", list(dependencies), tools=registry, provider=llm
    )


def test_it_writes_runs_and_reports_with_the_real_output_in_front_of_the_model():
    llm = FakeProvider([run_step("print(sum(range(10)))"), final("code + output 45")])
    result = go(llm)

    assert result.output == "code + output 45"
    assert (result.steps, result.tool_calls) == (2, 1)
    second = llm.calls[1]["prompt"]
    assert "you called run_python" in second
    assert "exit code 0" in second
    assert "45" in second  # the real stdout reached the model


def test_it_sees_the_tool_and_is_told_to_run_before_answering():
    llm = FakeProvider([run_step("print(1)"), final("ok")])
    go(llm)
    system = llm.calls[0]["system"]
    assert '"name": "run_python"' in system
    assert "ALWAYS run your code" in system


def test_it_can_read_a_traceback_and_fix_its_code():
    llm = FakeProvider([run_step("print(1 / 0)"), run_step("print(1 / 1)"), final("fixed")])
    result = go(llm)

    assert result.output == "fixed"
    assert result.tool_calls == 2
    assert "ZeroDivisionError" in llm.calls[1]["prompt"]
    assert "exit code 0" in llm.calls[2]["prompt"]


def test_it_may_not_report_results_it_never_produced():
    llm = FakeProvider([final("the answer is 42"), run_step("print(42)"), final("42, verified")])
    result = go(llm)

    assert result.output == "42, verified"
    assert "have not successfully run any code" in llm.calls[1]["prompt"]


def test_a_coder_that_only_ever_makes_up_answers_fails():
    llm = FakeProvider([final("trust me")] * CODER.max_steps)
    with pytest.raises(AgentError) as exc:
        go(llm)
    assert "no final answer" in str(exc.value)
    assert "have not successfully run any code" in str(exc.value)


def test_an_invalid_tool_call_does_not_count_as_running_code():
    bad = json.dumps({"action": "tool", "tool": "run_python", "args": {}})  # no code
    llm = FakeProvider([bad, final("pretend"), run_step("print(7)"), final("7")])
    result = go(llm)

    assert result.output == "7"
    assert "invalid arguments for run_python" in llm.calls[1]["prompt"]
    assert "rejected" in llm.calls[2]["prompt"]


def test_without_the_code_execute_capability_it_has_no_tool_and_cannot_finish():
    llm = FakeProvider([final("guess")] * CODER.max_steps)
    with pytest.raises(AgentError):
        go(llm, capabilities=())
    assert '"name": "run_python"' not in llm.calls[0]["system"]  # no tool schema was offered
    assert "You have no tools" in llm.calls[0]["system"]


def test_a_forbidden_tool_call_is_refused_and_explained():
    attempt = json.dumps({"action": "tool", "tool": "web_search", "args": {"query": "x"}})
    llm = FakeProvider([attempt, run_step("print(1)"), final("ok")])
    go(llm)
    assert "lacks capability 'web.search'" in llm.calls[1]["prompt"]


def test_it_asks_for_the_coding_model():
    assert CODER.task_type is TaskType.CODING


def test_dependency_outputs_are_given_as_data():
    llm = FakeProvider([run_step("print(1)"), final("ok")])
    deps = [DependencyOutput("t1", "find numbers", "use 1, 2, 3")]
    go(llm, dependencies=deps)
    assert "use 1, 2, 3" in llm.calls[0]["prompt"]
    assert "not instructions" in llm.calls[0]["prompt"]


def test_the_coder_and_reviewer_are_registered_as_runners():
    runners = build_default_runners()
    assert runners["coder"] is run_coder
    assert set(runners) == {"researcher", "coder", "reviewer"}
