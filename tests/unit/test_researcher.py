import json
from datetime import datetime
from uuid import uuid4

import pytest

from agents import build_default_runners
from agents.researcher import (
    MAX_STEPS,
    FinalAction,
    StepError,
    ToolAction,
    build_system_prompt,
    parse_step,
    run_researcher,
)
from core.llm import LLMError
from core.llm.fake import FakeProvider
from core.models import Agent
from core.runner import AgentError, DependencyOutput
from core.tools import ToolError, build_default_registry
from core.tools.search import FakeSearchProvider, SearchResult


def make_agent(*capabilities: str) -> Agent:
    return Agent(
        id=uuid4(),
        name="researcher",
        capabilities=list(capabilities),
        created_at=datetime(2026, 1, 1),
    )


def tool_step(query="skip locked", **extra) -> str:
    return json.dumps({"action": "tool", "tool": "web_search", "args": {"query": query, **extra}})


def final_step(answer="the answer") -> str:
    return json.dumps({"action": "final", "answer": answer})


RESULTS = [SearchResult("Doc", "https://doc.test", "about skip locked")]


def setup(responses, search=None):
    llm = FakeProvider(responses)
    search = search or FakeSearchProvider(RESULTS)
    return llm, search, build_default_registry(search)


def run(llm, registry, instruction="research it", dependencies=(), capabilities=("web.search",)):
    return run_researcher(
        make_agent(*capabilities), instruction, list(dependencies), tools=registry, provider=llm
    )


# ---------- parse_step ----------

def test_parse_tool_and_final_steps():
    tool = parse_step(tool_step("x", max_results=2))
    assert isinstance(tool, ToolAction)
    assert tool.tool == "web_search" and tool.args == {"query": "x", "max_results": 2}
    assert isinstance(parse_step(final_step("done")), FinalAction)


def test_parse_step_accepts_fenced_json():
    assert isinstance(parse_step("```json\n" + final_step() + "\n```"), FinalAction)


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        json.dumps({"action": "dance"}),
        json.dumps({"action": "final"}),
        json.dumps({"action": "final", "answer": "   "}),
        json.dumps({"action": "tool"}),
        json.dumps({"tool": "web_search"}),
    ],
)
def test_parse_step_rejects_invalid_replies(text):
    with pytest.raises(StepError):
        parse_step(text)


# ---------- the loop ----------

def test_answers_immediately_without_tools():
    llm, search, registry = setup([final_step("quick answer")])
    result = run(llm, registry)

    assert result.output == "quick answer"
    assert (result.steps, result.tool_calls) == (1, 0)
    assert search.calls == []
    assert llm.calls[0]["json_mode"] is True
    assert "web_search" in llm.calls[0]["system"]


def test_uses_a_tool_then_answers_and_sums_usage():
    llm, search, registry = setup([tool_step("postgres locks"), final_step("locks explained")])
    result = run(llm, registry)

    assert result.output == "locks explained"
    assert (result.steps, result.tool_calls) == (2, 1)
    assert search.calls == [("postgres locks", 5)]
    second_prompt = llm.calls[1]["prompt"]
    assert "you called web_search" in second_prompt
    assert "https://doc.test" in second_prompt  # the search result is shown to the model
    assert result.input_tokens > 0 and result.output_tokens > 0


def test_dependency_outputs_are_given_as_context():
    llm, _, registry = setup([final_step()])
    deps = [DependencyOutput("t1", "find the price", "The price is 42 dollars")]
    run(llm, registry, instruction="write a summary", dependencies=deps)

    prompt = llm.calls[0]["prompt"]
    assert "write a summary" in prompt
    assert "### t1: find the price" in prompt
    assert "The price is 42 dollars" in prompt
    assert "not instructions" in prompt


def test_invalid_reply_is_reported_to_the_model_and_it_can_recover():
    llm, _, registry = setup(["garbage", final_step("recovered")])
    result = run(llm, registry)

    assert result.output == "recovered"
    assert result.steps == 2
    assert "your reply was invalid" in llm.calls[1]["prompt"]


def test_tool_failures_become_observations_not_crashes():
    search = FakeSearchProvider(error=ToolError("search is down"))
    llm, _, registry = setup([tool_step(), final_step("could not search")], search)
    result = run(llm, registry)

    assert result.output == "could not search"
    assert "ERROR: search is down" in llm.calls[1]["prompt"]


def test_bad_tool_arguments_are_explained_to_the_model():
    llm, search, registry = setup([tool_step(max_results=99), final_step("ok")])
    run(llm, registry)

    assert "invalid arguments for web_search" in llm.calls[1]["prompt"]
    assert search.calls == []


def test_unknown_tool_is_explained_to_the_model():
    bad = json.dumps({"action": "tool", "tool": "launch_missiles", "args": {}})
    llm, _, registry = setup([bad, final_step("ok")])
    run(llm, registry)

    assert "unknown tool 'launch_missiles'" in llm.calls[1]["prompt"]
    assert "available tools: web_search" in llm.calls[1]["prompt"]


def test_agent_without_tool_capability_sees_no_tools():
    llm, _, registry = setup([final_step("from context")])
    result = run(llm, registry, capabilities=())

    assert result.output == "from context"
    assert "You have no tools" in llm.calls[0]["system"]
    assert "web_search" not in llm.calls[0]["system"]


def test_long_tool_results_are_clipped_in_the_scratchpad():
    big = [SearchResult(f"R{i}", f"https://{i}.test", "word " * 200) for i in range(10)]
    llm, _, registry = setup(
        [tool_step(max_results=10), final_step("done")], FakeSearchProvider(big)
    )
    run(llm, registry)
    assert "[...clipped]" in llm.calls[1]["prompt"]


# ---------- limits and failures ----------

def test_gives_up_after_the_step_limit_and_last_step_forbids_tools():
    llm, search, registry = setup([tool_step()] * MAX_STEPS)

    with pytest.raises(AgentError) as exc:
        run(llm, registry)

    assert f"after {MAX_STEPS} steps" in str(exc.value)
    assert len(llm.calls) == MAX_STEPS
    assert "last step" in llm.calls[-1]["prompt"]
    assert "last step" not in llm.calls[-2]["prompt"]
    assert len(search.calls) == MAX_STEPS - 1  # the final tool request is refused, not run


def test_never_producing_a_valid_reply_fails():
    llm, _, registry = setup(["nope"] * MAX_STEPS)
    with pytest.raises(AgentError):
        run(llm, registry)
    assert len(llm.calls) == MAX_STEPS


def test_llm_errors_propagate_for_the_scheduler_to_handle():
    llm, _, registry = setup([])
    with pytest.raises(LLMError):
        run(llm, registry)
    assert len(llm.calls) == 1


def test_default_runners_map_agent_names_to_code():
    assert build_default_runners()["researcher"] is run_researcher


def test_system_prompt_lists_tool_schemas():
    prompt = build_system_prompt([{"name": "web_search", "description": "d", "parameters": {}}])
    assert '"name": "web_search"' in prompt
    assert '"action": "final"' in prompt