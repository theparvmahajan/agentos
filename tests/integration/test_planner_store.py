import json

import pytest

from core import repository as repo
from core.db import get_conn
from core.llm.fake import FakeProvider
from core.models import MissionStatus, TaskStatus
from core.planner import Plan, PlanningError, PlanTask, run_planner, store_plan


def test_store_plan_creates_tasks_and_dependency_edges(conn):
    agent = repo.create_agent(conn, "researcher")
    mission = repo.create_mission(conn, "x")
    plan = Plan(
        tasks=[
            PlanTask(key="t1", agent="researcher", instruction="first"),
            PlanTask(key="t2", agent="researcher", instruction="second"),
            PlanTask(
                key="t3", agent="researcher", instruction="third", depends_on=["t1", "t2"]
            ),
        ]
    )

    created = store_plan(conn, mission.id, plan, [agent])

    assert [t.key for t in created] == ["t1", "t2", "t3"]
    stored = {t.key: t for t in repo.get_tasks(conn, mission.id)}
    assert stored["t1"].depends_on == []
    assert set(stored["t3"].depends_on) == {stored["t1"].id, stored["t2"].id}
    assert all(t.status == TaskStatus.PENDING for t in stored.values())


# run_planner opens its own connections and commits, so these tests clean up by hand.

@pytest.fixture
def committed(test_db_url):
    with get_conn(test_db_url) as conn:
        agent = repo.create_agent(conn, "planner-test-agent", description="test agent")
        mission = repo.create_mission(conn, "do the thing")
    yield agent, mission
    with get_conn(test_db_url) as conn:
        conn.execute("DELETE FROM missions WHERE id = %s", (mission.id,))
        conn.execute("DELETE FROM agents WHERE id = %s", (agent.id,))


def good_plan() -> str:
    return json.dumps(
        {
            "tasks": [
                {
                    "key": "t1",
                    "agent": "planner-test-agent",
                    "instruction": "research",
                    "depends_on": [],
                },
                {
                    "key": "t2",
                    "agent": "planner-test-agent",
                    "instruction": "summarize",
                    "depends_on": ["t1"],
                },
            ]
        }
    )


def test_run_planner_stores_tasks_and_marks_mission_running(test_db_url, committed):
    _, mission = committed
    fake = FakeProvider([good_plan()])

    tasks = run_planner(mission.id, provider=fake, database_url=test_db_url)

    assert [t.key for t in tasks] == ["t1", "t2"]
    with get_conn(test_db_url) as conn:
        assert repo.get_mission(conn, mission.id).status == MissionStatus.RUNNING
        stored = {t.key: t for t in repo.get_tasks(conn, mission.id)}
    assert stored["t2"].depends_on == [stored["t1"].id]


def test_run_planner_marks_mission_failed_when_planning_fails(test_db_url, committed):
    _, mission = committed
    fake = FakeProvider(["garbage", "more garbage"])

    with pytest.raises(PlanningError):
        run_planner(mission.id, provider=fake, database_url=test_db_url)

    with get_conn(test_db_url) as conn:
        failed = repo.get_mission(conn, mission.id)
        assert failed.status == MissionStatus.FAILED
        assert "planning failed" in failed.error
        assert repo.get_tasks(conn, mission.id) == []


def test_run_planner_refuses_a_mission_that_is_not_pending(test_db_url, committed):
    _, mission = committed
    run_planner(mission.id, provider=FakeProvider([good_plan()]), database_url=test_db_url)

    with pytest.raises(ValueError):
        run_planner(mission.id, provider=FakeProvider([good_plan()]), database_url=test_db_url)


def test_run_planner_unknown_mission(test_db_url):
    import uuid

    with pytest.raises(LookupError):
        run_planner(uuid.uuid4(), provider=FakeProvider([]), database_url=test_db_url)