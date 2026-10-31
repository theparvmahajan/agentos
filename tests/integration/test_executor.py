import json

import pytest

from agents import run_researcher
from core import repository as repo
from core.db import get_conn
from core.executor import execute_mission
from core.llm.fake import FakeProvider
from core.models import MissionStatus, TaskStatus
from core.planner import run_planner
from core.runner import AgentResult
from core.tools import ToolRegistry, build_default_registry
from core.tools.search import FakeSearchProvider, SearchResult

AGENT_NAME = "executor-test-agent"


class Recorder:
    """A fake runner: records what it was asked to do, can be told to fail."""

    def __init__(self, fail_on=()):
        self.fail_on = set(fail_on)
        self.calls = []  # (instruction, dependencies)

    def __call__(self, agent, instruction, dependencies, *, tools, provider=None):
        self.calls.append((instruction, list(dependencies)))
        if instruction in self.fail_on:
            raise RuntimeError("boom")
        return AgentResult(output=f"result of {instruction}")

    @property
    def order(self):
        return [instruction for instruction, _ in self.calls]


@pytest.fixture
def make_mission(test_db_url):
    """Committed data (the executor opens its own connections), cleaned up afterwards."""
    created = {"missions": [], "agent": None}

    def _make(spec, status=MissionStatus.RUNNING):
        # spec: [(key, instruction, [dependency keys])], dependencies listed first
        with get_conn(test_db_url) as conn:
            if created["agent"] is None:
                agent = repo.create_agent(conn, AGENT_NAME, capabilities=["web.search"])
                created["agent"] = agent.id
            mission = repo.create_mission(conn, "objective")
            ids = {}
            for key, instruction, deps in spec:
                task = repo.create_task(
                    conn,
                    mission.id,
                    key,
                    created["agent"],
                    instruction,
                    depends_on=[ids[d] for d in deps],
                )
                ids[key] = task.id
            repo.set_mission_status(conn, mission.id, status)
        created["missions"].append(mission.id)
        return mission.id

    yield _make

    with get_conn(test_db_url) as conn:
        for mission_id in created["missions"]:
            conn.execute("DELETE FROM missions WHERE id = %s", (mission_id,))
        if created["agent"] is not None:
            conn.execute("DELETE FROM agents WHERE id = %s", (created["agent"],))


def run(test_db_url, mission_id, runner):
    return execute_mission(
        mission_id,
        runners={AGENT_NAME: runner},
        tools=ToolRegistry(),
        database_url=test_db_url,
    )


def tasks_by_key(test_db_url, mission_id):
    with get_conn(test_db_url) as conn:
        return {t.key: t for t in repo.get_tasks(conn, mission_id)}


# ---------- happy paths ----------

def test_chain_runs_in_order_and_passes_outputs_forward(test_db_url, make_mission):
    mission_id = make_mission([("t1", "first", []), ("t2", "second", ["t1"])])
    runner = Recorder()

    mission = run(test_db_url, mission_id, runner)

    assert runner.order == ["first", "second"]
    second_deps = runner.calls[1][1]
    assert [(d.key, d.instruction, d.output) for d in second_deps] == [
        ("t1", "first", "result of first")
    ]
    assert mission.status == MissionStatus.COMPLETED
    assert mission.result == "result of second"  # the one task nothing depends on
    assert mission.completed_at is not None

    tasks = tasks_by_key(test_db_url, mission_id)
    for task in tasks.values():
        assert task.status == TaskStatus.COMPLETED
        assert task.attempts == 1
        assert task.started_at is not None and task.finished_at is not None
    assert tasks["t1"].output == "result of first"


def test_diamond_respects_dependencies(test_db_url, make_mission):
    mission_id = make_mission(
        [
            ("a", "A", []),
            ("b", "B", ["a"]),
            ("c", "C", ["a"]),
            ("d", "D", ["b", "c"]),
        ]
    )
    runner = Recorder()
    mission = run(test_db_url, mission_id, runner)

    order = runner.order
    assert order[0] == "A" and order[-1] == "D"
    assert set(order[1:3]) == {"B", "C"}
    assert {d.key for d in runner.calls[-1][1]} == {"b", "c"}
    assert mission.result == "result of D"


def test_execution_order_does_not_depend_on_database_row_order(test_db_url, make_mission):
    # Tasks created in one transaction share a timestamp, so the database returns them
    # sorted by key: "a_summary" comes back BEFORE "z_research" even though it depends on it.
    mission_id = make_mission([("z_research", "research", []), ("a_summary", "summary", ["z_research"])])
    runner = Recorder()

    mission = run(test_db_url, mission_id, runner)

    assert runner.order == ["research", "summary"]
    assert mission.status == MissionStatus.COMPLETED


def test_several_final_tasks_are_combined_in_the_result(test_db_url, make_mission):
    mission_id = make_mission([("x", "X", []), ("y", "Y", [])])
    mission = run(test_db_url, mission_id, Recorder())

    assert "## x\nresult of X" in mission.result
    assert "## y\nresult of Y" in mission.result


# ---------- failures ----------

def test_a_failure_skips_dependents_but_independent_branches_finish(test_db_url, make_mission):
    mission_id = make_mission(
        [
            ("t1", "ok first", []),
            ("t2", "will fail", ["t1"]),
            ("t3", "needs t2", ["t2"]),
            ("t4", "independent", ["t1"]),
        ]
    )
    runner = Recorder(fail_on={"will fail"})

    mission = run(test_db_url, mission_id, runner)

    tasks = tasks_by_key(test_db_url, mission_id)
    assert tasks["t1"].status == TaskStatus.COMPLETED
    assert tasks["t2"].status == TaskStatus.FAILED
    assert "RuntimeError: boom" in tasks["t2"].last_error
    assert tasks["t3"].status == TaskStatus.SKIPPED
    assert tasks["t4"].status == TaskStatus.COMPLETED  # not blocked by t2

    assert "needs t2" not in runner.order  # a skipped task never runs
    assert mission.status == MissionStatus.FAILED
    assert "t2" in mission.error and "boom" in mission.error and "skipped: t3" in mission.error


def test_missing_runner_fails_the_task_with_a_clear_message(test_db_url, make_mission):
    mission_id = make_mission([("t1", "work", [])])
    mission = execute_mission(
        mission_id, runners={}, tools=ToolRegistry(), database_url=test_db_url
    )

    task = tasks_by_key(test_db_url, mission_id)["t1"]
    assert task.status == TaskStatus.FAILED
    assert f"no runner registered for agent '{AGENT_NAME}'" in task.last_error
    assert mission.status == MissionStatus.FAILED


# ---------- refusals ----------

def test_refuses_a_mission_that_is_not_running(test_db_url, make_mission):
    mission_id = make_mission([("t1", "work", [])], status=MissionStatus.PENDING)
    with pytest.raises(ValueError, match="expected running"):
        run(test_db_url, mission_id, Recorder())


def test_refuses_to_rerun_a_mission_that_already_started(test_db_url, make_mission):
    mission_id = make_mission([("t1", "work", [])])
    run(test_db_url, mission_id, Recorder())

    with get_conn(test_db_url) as conn:  # pretend it is running again
        repo.set_mission_status(conn, mission_id, MissionStatus.RUNNING)
    with pytest.raises(ValueError, match="already started"):
        run(test_db_url, mission_id, Recorder())


def test_unknown_mission(test_db_url):
    import uuid

    with pytest.raises(LookupError):
        run(test_db_url, uuid.uuid4(), Recorder())


# ---------- the whole stack ----------

def test_plan_then_execute_with_the_real_researcher(test_db_url, make_mission):
    # make_mission creates the committed agent; the planner then plans for it.
    mission_id = make_mission([("seed", "unused", [])], status=MissionStatus.PENDING)
    with get_conn(test_db_url) as conn:
        conn.execute("DELETE FROM tasks WHERE mission_id = %s", (mission_id,))

    plan = json.dumps(
        {
            "tasks": [
                {"key": "t1", "agent": AGENT_NAME, "instruction": "find facts", "depends_on": []},
                {"key": "t2", "agent": AGENT_NAME, "instruction": "summarize", "depends_on": ["t1"]},
            ]
        }
    )
    run_planner(mission_id, provider=FakeProvider([plan]), database_url=test_db_url)

    search = FakeSearchProvider([SearchResult("Doc", "https://doc.test", "fact one")])
    researcher_llm = FakeProvider(
        [
            json.dumps({"action": "tool", "tool": "web_search", "args": {"query": "facts"}}),
            json.dumps({"action": "final", "answer": "found: fact one"}),
            json.dumps({"action": "final", "answer": "summary: fact one matters"}),
        ]
    )
    mission = execute_mission(
        mission_id,
        runners={AGENT_NAME: run_researcher},
        tools=build_default_registry(search),
        provider=researcher_llm,
        database_url=test_db_url,
    )

    assert mission.status == MissionStatus.COMPLETED
    assert mission.result == "summary: fact one matters"
    assert search.calls == [("facts", 5)]
    # t2's agent was shown t1's output as context
    assert "found: fact one" in researcher_llm.calls[2]["prompt"]