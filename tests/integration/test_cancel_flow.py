"""Cancelling while other processes are in the middle of work. Each test freezes one of them
at the dangerous moment, cancels, lets it continue, and checks the cancellation survived."""

import json
import threading

import pytest

from core import repository as repo
from core.db import get_conn
from core.llm.fake import FakeProvider
from core.models import MissionStatus, TaskStatus
from core.pipeline import plan_and_publish
from core.planner import run_planner
from core.runner import AgentResult
from core.scheduler import schedule_mission
from core.tools import ToolRegistry
from core.worker import Outcome, handle_task_message

AGENT = "cancel-flow-agent"


class BlockingProvider(FakeProvider):
    """Waits inside the LLM call until the test lets it go: a planner frozen mid-plan."""

    def __init__(self, responses):
        super().__init__(responses)
        self.entered = threading.Event()
        self.release = threading.Event()

    def complete(self, **kwargs):
        self.entered.set()
        assert self.release.wait(10), "the test never released the provider"
        return super().complete(**kwargs)


def plan_json() -> str:
    return json.dumps({"tasks": [
        {"key": "t1", "agent": AGENT, "instruction": "work", "depends_on": []},
        {"key": "t2", "agent": AGENT, "instruction": "more", "depends_on": ["t1"]},
    ]})


@pytest.fixture
def agent_id(test_db_url):
    with get_conn(test_db_url) as conn:
        agent = repo.create_agent(conn, AGENT)
    yield agent.id
    with get_conn(test_db_url) as conn:
        conn.execute("DELETE FROM missions WHERE objective LIKE '[cancel-flow]%'")
        conn.execute("DELETE FROM agents WHERE id = %s", (agent.id,))


def new_mission(test_db_url, status=MissionStatus.PENDING):
    with get_conn(test_db_url) as conn:
        mission = repo.create_mission(conn, "[cancel-flow] x")
        repo.set_mission_status(conn, mission.id, status)
    return mission.id


def running_mission_with_two_tasks(test_db_url, agent_id):
    mission_id = new_mission(test_db_url, MissionStatus.RUNNING)
    with get_conn(test_db_url) as conn:
        first = repo.create_task(conn, mission_id, "a", agent_id, "do a")
        repo.create_task(conn, mission_id, "b", agent_id, "do b", depends_on=[first.id])
    return mission_id


def state(test_db_url, mission_id):
    with get_conn(test_db_url) as conn:
        return repo.get_mission(conn, mission_id), {
            t.key: t for t in repo.get_tasks(conn, mission_id)
        }


def cancel(test_db_url, mission_id):
    with get_conn(test_db_url) as conn:
        return repo.cancel_mission(conn, mission_id)


def events(queues):
    return [f for _, f in queues.events.client.xrange(queues.events.name)]


def in_background(fn):
    result = {}

    def run():
        try:
            result["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - reported to the test thread
            result["error"] = e

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


# ---------- the planner ----------

def test_a_mission_cancelled_while_it_is_being_planned_stays_cancelled_and_is_never_scheduled(
    test_db_url, queues, agent_id
):
    mission_id = new_mission(test_db_url)
    llm = BlockingProvider([plan_json()])
    thread, outcome = in_background(
        lambda: plan_and_publish(mission_id, queues=queues, provider=llm, database_url=test_db_url)
    )
    assert llm.entered.wait(5)  # the planner is now inside the LLM call, status 'planning'
    assert cancel(test_db_url, mission_id).status == MissionStatus.CANCELLED
    llm.release.set()
    thread.join(10)

    mission, tasks = state(test_db_url, mission_id)
    assert "error" not in outcome  # the job never raises
    assert mission.status == MissionStatus.CANCELLED  # NOT overwritten with 'running'
    assert tasks == {}  # the finished plan was thrown away
    assert events(queues) == []  # and the scheduler was not woken for it


def test_a_planner_that_fails_after_a_cancellation_does_not_turn_it_into_a_failure(
    test_db_url, queues, agent_id
):
    mission_id = new_mission(test_db_url)
    llm = BlockingProvider(["garbage", "more garbage"])
    thread, _ = in_background(
        lambda: plan_and_publish(mission_id, queues=queues, provider=llm, database_url=test_db_url)
    )
    assert llm.entered.wait(5)
    cancel(test_db_url, mission_id)
    llm.release.set()
    thread.join(10)

    mission, _ = state(test_db_url, mission_id)
    assert mission.status == MissionStatus.CANCELLED and mission.error is None


def test_only_one_of_two_simultaneous_planners_gets_to_plan(test_db_url, agent_id):
    mission_id = new_mission(test_db_url)
    llm = BlockingProvider([plan_json()])
    first, outcome = in_background(
        lambda: run_planner(mission_id, provider=llm, database_url=test_db_url)
    )
    assert llm.entered.wait(5)  # the first planner owns the mission

    with pytest.raises(ValueError, match="expected pending"):
        run_planner(mission_id, provider=FakeProvider([plan_json()]), database_url=test_db_url)

    llm.release.set()
    first.join(10)
    mission, tasks = state(test_db_url, mission_id)
    assert mission.status == MissionStatus.RUNNING and set(tasks) == {"t1", "t2"}
    assert len(outcome["value"]) == 2


# ---------- the scheduler and the workers ----------

def test_the_scheduler_ignores_a_cancelled_mission(test_db_url, queues, agent_id):
    mission_id = running_mission_with_two_tasks(test_db_url, agent_id)
    cancel(test_db_url, mission_id)

    schedule_mission(mission_id, queues, database_url=test_db_url)

    _, tasks = state(test_db_url, mission_id)
    assert {t.status for t in tasks.values()} == {TaskStatus.CANCELLED}
    assert queues.tasks.length() == 0  # nothing was queued


def test_a_task_that_was_queued_and_then_cancelled_is_not_run(test_db_url, queues, agent_id):
    mission_id = running_mission_with_two_tasks(test_db_url, agent_id)
    schedule_mission(mission_id, queues, database_url=test_db_url)  # 'a' is READY and queued
    cancel(test_db_url, mission_id)
    ran = []

    def runner(*a, **k):
        ran.append(1)
        return AgentResult(output="should not exist")

    [message] = queues.tasks.read("w")
    outcome = handle_task_message(
        message, queues, runners={AGENT: runner}, tools=ToolRegistry(), database_url=test_db_url
    )

    assert outcome == Outcome.DUPLICATE and ran == []
    assert queues.tasks.pending_count() == 0  # acknowledged, not retried forever


def test_a_task_cancelled_while_it_runs_has_its_result_discarded_quietly(
    test_db_url, queues, agent_id
):
    mission_id = running_mission_with_two_tasks(test_db_url, agent_id)
    schedule_mission(mission_id, queues, database_url=test_db_url)
    started, release = threading.Event(), threading.Event()

    def slow_runner(*a, **k):
        started.set()
        assert release.wait(10)
        return AgentResult(output="finished after the cancel")

    [message] = queues.tasks.read("w")
    thread, outcome = in_background(
        lambda: handle_task_message(
            message, queues, runners={AGENT: slow_runner}, tools=ToolRegistry(),
            database_url=test_db_url,
        )
    )
    assert started.wait(5)  # the task is RUNNING right now
    cancel(test_db_url, mission_id)
    release.set()
    thread.join(10)

    _, tasks = state(test_db_url, mission_id)
    assert "error" not in outcome and outcome["value"] == Outcome.DISCARDED
    assert tasks["a"].status == TaskStatus.CANCELLED and tasks["a"].output is None
    assert queues.tasks.pending_count() == 0  # acknowledged: no endless redelivery
    assert len(events(queues)) == 1  # announced, so the scheduler can settle the mission


def test_a_task_that_fails_after_being_cancelled_is_also_discarded(test_db_url, queues, agent_id):
    mission_id = running_mission_with_two_tasks(test_db_url, agent_id)
    schedule_mission(mission_id, queues, database_url=test_db_url)
    started, release = threading.Event(), threading.Event()

    def failing_runner(*a, **k):
        started.set()
        assert release.wait(10)
        raise RuntimeError("boom")

    [message] = queues.tasks.read("w")
    thread, outcome = in_background(
        lambda: handle_task_message(
            message, queues, runners={AGENT: failing_runner}, tools=ToolRegistry(),
            database_url=test_db_url,
        )
    )
    assert started.wait(5)
    cancel(test_db_url, mission_id)
    release.set()
    thread.join(10)

    _, tasks = state(test_db_url, mission_id)
    assert outcome["value"] == Outcome.DISCARDED
    assert tasks["a"].status == TaskStatus.CANCELLED and tasks["a"].last_error is None
