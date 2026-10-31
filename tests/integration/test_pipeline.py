import json

import pytest

from agents import run_researcher
from core import pipeline
from core import repository as repo
from core.db import get_conn
from core.llm.fake import FakeProvider
from core.models import MissionStatus
from core.pipeline import process_mission
from core.tools import ToolRegistry

AGENT_NAME = "pipeline-test-agent"


@pytest.fixture
def pending_mission(test_db_url):
    with get_conn(test_db_url) as conn:
        agent = repo.create_agent(conn, AGENT_NAME, capabilities=[])
        mission = repo.create_mission(conn, "do the thing")
    yield mission.id
    with get_conn(test_db_url) as conn:
        conn.execute("DELETE FROM missions WHERE id = %s", (mission.id,))
        conn.execute("DELETE FROM agents WHERE id = %s", (agent.id,))


def one_task_plan() -> str:
    return json.dumps(
        {"tasks": [{"key": "t1", "agent": AGENT_NAME, "instruction": "work", "depends_on": []}]}
    )


def final(answer: str) -> str:
    return json.dumps({"action": "final", "answer": answer})


def run(test_db_url, mission_id, provider):
    process_mission(
        mission_id,
        runners={AGENT_NAME: run_researcher},
        tools=ToolRegistry(),
        provider=provider,
        database_url=test_db_url,
    )


def current(test_db_url, mission_id):
    with get_conn(test_db_url) as conn:
        return repo.get_mission(conn, mission_id)


def test_plans_and_executes_a_mission_end_to_end(test_db_url, pending_mission):
    run(test_db_url, pending_mission, FakeProvider([one_task_plan(), final("all done")]))

    mission = current(test_db_url, pending_mission)
    assert mission.status == MissionStatus.COMPLETED
    assert mission.result == "all done"


def test_a_planning_failure_is_recorded_not_raised(test_db_url, pending_mission):
    run(test_db_url, pending_mission, FakeProvider(["garbage", "more garbage"]))  # must not raise

    mission = current(test_db_url, pending_mission)
    assert mission.status == MissionStatus.FAILED
    assert "planning failed" in mission.error


def test_an_unexpected_executor_crash_fails_the_mission_instead_of_leaving_it_running(
    test_db_url, pending_mission, monkeypatch
):
    def explode(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(pipeline, "execute_mission", explode)

    run(test_db_url, pending_mission, FakeProvider([one_task_plan()]))  # must not raise

    mission = current(test_db_url, pending_mission)
    assert mission.status == MissionStatus.FAILED
    assert "internal error" in mission.error and "db exploded" in mission.error


def test_a_mission_that_is_not_pending_is_left_alone(test_db_url, pending_mission):
    with get_conn(test_db_url) as conn:
        repo.set_mission_status(conn, pending_mission, MissionStatus.RUNNING)

    run(test_db_url, pending_mission, FakeProvider([one_task_plan()]))  # must not raise

    assert current(test_db_url, pending_mission).status == MissionStatus.RUNNING