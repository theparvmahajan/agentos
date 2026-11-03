import json

import pytest

from core import pipeline
from core import repository as repo
from core.db import get_conn
from core.llm.fake import FakeProvider
from core.models import MissionStatus, TaskStatus
from core.pipeline import plan_and_publish
from core.queue import Queues

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


def two_task_plan() -> str:
    return json.dumps({"tasks": [
        {"key": "t1", "agent": AGENT_NAME, "instruction": "work", "depends_on": []},
        {"key": "t2", "agent": AGENT_NAME, "instruction": "more", "depends_on": ["t1"]},
    ]})


def launch(test_db_url, queues, mission_id, provider):
    plan_and_publish(mission_id, queues=queues, provider=provider, database_url=test_db_url)


def current(test_db_url, mission_id):
    with get_conn(test_db_url) as conn:
        return repo.get_mission(conn, mission_id), repo.get_tasks(conn, mission_id)


def events(queues):
    return [f for _, f in queues.events.client.xrange(queues.events.name)]


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(pipeline.time, "sleep", lambda _s: None)


def test_it_plans_the_mission_and_wakes_the_scheduler_exactly_once(
    test_db_url, queues, pending_mission
):
    launch(test_db_url, queues, pending_mission, FakeProvider([two_task_plan()]))

    mission, tasks = current(test_db_url, pending_mission)
    assert mission.status == MissionStatus.RUNNING  # planned, and NOT executed here
    assert {t.status for t in tasks} == {TaskStatus.PENDING}
    assert events(queues) == [{"mission_id": str(pending_mission)}]


def test_a_planning_failure_is_recorded_and_nothing_is_published(
    test_db_url, queues, pending_mission
):
    launch(test_db_url, queues, pending_mission, FakeProvider(["garbage", "more garbage"]))  # no raise

    mission, _ = current(test_db_url, pending_mission)
    assert mission.status == MissionStatus.FAILED
    assert "planning failed" in mission.error
    assert events(queues) == []


def test_a_mission_that_is_not_pending_is_left_alone(test_db_url, queues, pending_mission):
    with get_conn(test_db_url) as conn:
        repo.set_mission_status(conn, pending_mission, MissionStatus.RUNNING)

    launch(test_db_url, queues, pending_mission, FakeProvider([two_task_plan()]))  # no raise

    assert current(test_db_url, pending_mission)[0].status == MissionStatus.RUNNING
    assert events(queues) == []


def test_a_brief_redis_outage_is_retried(test_db_url, queues, pending_mission, monkeypatch):
    real = Queues.publish_event
    attempts = []

    def flaky(self, *args, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("redis hiccup")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Queues, "publish_event", flaky)
    launch(test_db_url, queues, pending_mission, FakeProvider([two_task_plan()]))

    assert len(attempts) == 3
    assert current(test_db_url, pending_mission)[0].status == MissionStatus.RUNNING
    assert len(events(queues)) == 1


def test_if_the_scheduler_can_never_be_reached_the_mission_fails_instead_of_hanging(
    test_db_url, queues, pending_mission, monkeypatch
):
    def down(self, *args, **kwargs):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(Queues, "publish_event", down)
    launch(test_db_url, queues, pending_mission, FakeProvider([two_task_plan()]))  # no raise

    mission, tasks = current(test_db_url, pending_mission)
    assert mission.status == MissionStatus.FAILED
    assert "could not hand the mission to the scheduler" in mission.error
    assert "redis is down" in mission.error
    assert {t.status for t in tasks} == {TaskStatus.CANCELLED}  # nothing left looking runnable


def test_it_gives_up_after_three_attempts(test_db_url, queues, pending_mission, monkeypatch):
    attempts = []

    def down(self, *args, **kwargs):
        attempts.append(1)
        raise ConnectionError("down")

    monkeypatch.setattr(Queues, "publish_event", down)
    launch(test_db_url, queues, pending_mission, FakeProvider([two_task_plan()]))
    assert len(attempts) == 3
