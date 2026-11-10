"""Failure injection: a message or a whole process is lost and a mission would wait forever.

The mission sweep is the safety net under the event stream: it looks at the database, not at
messages. Each test builds the exact state a specific failure leaves behind.
"""

import threading

import pytest

from core import repository as repo
from core.db import get_conn
from core.models import MissionStatus, TaskStatus
from core.planner import run_planner
from core.scheduler import run_scheduler, sweep_missions
from tests.integration.conftest import wait_until

STALE = 0.0  # in these tests every quiet mission counts as stalled; production uses 30 s


def mission_row(url, mission_id):
    with get_conn(url) as conn:
        return repo.get_mission(conn, mission_id)


def tasks_of(url, mission_id):
    with get_conn(url) as conn:
        return repo.get_tasks(conn, mission_id)


def set_task(url, task_id, status):
    with get_conn(url) as conn:
        conn.execute("UPDATE tasks SET status = %s, updated_at = now() WHERE id = %s", (status, task_id))


# ---------- running missions whose wake-up event was lost ----------

def test_a_lost_event_after_a_task_finished_is_repaired(test_db_url, queues, chain):
    """The worker saved task 0's result, then died before announcing it: nothing tells the
    scheduler that task 1 can start. (Redelivery would repair this if the worker came back; here
    nobody does.)"""
    mission_id, ids = chain(3)
    set_task(test_db_url, ids[0], "completed")

    report = sweep_missions(queues, stale_seconds=STALE, database_url=test_db_url)

    assert report.rescheduled == (mission_id,)
    assert tasks_of(test_db_url, mission_id)[1].status == TaskStatus.READY
    assert queues.tasks.length() == 1  # and it was queued


def test_a_lost_failed_event_still_finishes_the_mission(test_db_url, queues, chain):
    """The reaper failed a task for good and died before announcing it: dependents would stay
    pending and the mission running, forever."""
    mission_id, ids = chain(3)
    set_task(test_db_url, ids[0], "failed")

    sweep_missions(queues, stale_seconds=STALE, database_url=test_db_url)

    states = [t.status for t in tasks_of(test_db_url, mission_id)]
    assert states == [TaskStatus.FAILED, TaskStatus.SKIPPED, TaskStatus.SKIPPED]
    assert mission_row(test_db_url, mission_id).status == MissionStatus.FAILED


def test_a_mission_that_is_making_progress_is_left_alone(test_db_url, queues, chain):
    in_flight, ids = chain(2)
    set_task(test_db_url, ids[0], "running")  # something is executing
    recently_active, ids2 = chain(2)
    set_task(test_db_url, ids2[0], "completed")  # quiet for 0 s, and the stale limit below is 1 h

    report = sweep_missions(queues, stale_seconds=3600, database_url=test_db_url)
    assert report.rescheduled == ()

    report = sweep_missions(queues, stale_seconds=STALE, database_url=test_db_url)
    assert in_flight not in report.rescheduled  # a running task is the reaper's business
    assert recently_active in report.rescheduled


def test_sweeping_twice_does_not_queue_a_task_twice(test_db_url, queues, chain):
    _, ids = chain(2)
    set_task(test_db_url, ids[0], "completed")
    sweep_missions(queues, stale_seconds=STALE, database_url=test_db_url)
    sweep_missions(queues, stale_seconds=STALE, database_url=test_db_url)
    assert queues.tasks.length() == 1  # the second pass found the task already ready


def test_the_scheduler_loop_runs_the_mission_sweep(test_db_url, queues, chain):
    _, ids = chain(2)
    set_task(test_db_url, ids[0], "completed")  # and no event is ever published
    stop = threading.Event()
    thread = threading.Thread(target=run_scheduler, daemon=True, kwargs={
        "queues": queues, "consumer": "s", "stop": stop, "database_url": test_db_url, "block_ms": 100,
        "mission_sweep_interval": 0.1, "mission_stale_seconds": 0.0})
    thread.start()
    try:
        assert wait_until(lambda: queues.tasks.length() == 1, 10)
    finally:
        stop.set()
        thread.join(10)


# ---------- the API stopped before planning finished ----------

@pytest.fixture
def pending_mission(test_db_url):
    ids = []

    def make(status="pending"):
        with get_conn(test_db_url) as conn:
            mission = repo.create_mission(conn, "[chaos] stuck in planning")
            if status != "pending":
                repo.set_mission_status(conn, mission.id, MissionStatus(status))
        ids.append(mission.id)
        return mission.id

    yield make
    with get_conn(test_db_url) as conn:
        for mission_id in ids:
            conn.execute("DELETE FROM missions WHERE id = %s", (mission_id,))


@pytest.mark.parametrize("status", ["pending", "planning"])
def test_a_mission_whose_planner_never_finished_is_declared_failed(
    test_db_url, queues, pending_mission, status
):
    """The API process stopped after saving the mission (or mid-planning). Without this the
    mission would show 'pending' or 'planning' for ever."""
    mission_id = pending_mission(status)

    report = sweep_missions(queues, planning_stale_seconds=0.0, database_url=test_db_url)

    assert mission_id in report.failed
    mission = mission_row(test_db_url, mission_id)
    assert mission.status == MissionStatus.FAILED
    assert "planning never finished" in mission.error and mission.completed_at is not None


def test_a_young_pending_mission_is_not_touched(test_db_url, queues, pending_mission):
    mission_id = pending_mission()
    report = sweep_missions(queues, planning_stale_seconds=3600, database_url=test_db_url)
    assert mission_id not in report.failed
    assert mission_row(test_db_url, mission_id).status == MissionStatus.PENDING


def test_a_slow_planner_that_finishes_after_the_mission_was_failed_changes_nothing(
    test_db_url, queues, pending_mission
):
    """The watchdog was wrong (the planner was only slow). The late plan must be discarded, not
    resurrect the mission."""
    from core.llm.fake import FakeProvider

    mission_id = pending_mission("planning")
    sweep_missions(queues, planning_stale_seconds=0.0, database_url=test_db_url)
    assert mission_row(test_db_url, mission_id).status == MissionStatus.FAILED

    late_plan = FakeProvider(['{"tasks": []}'])
    try:
        tasks = run_planner(mission_id, provider=late_plan, database_url=test_db_url)
    except ValueError:
        tasks = []  # a planner that has not started yet refuses a mission that is no longer pending
    assert tasks == []
    assert mission_row(test_db_url, mission_id).status == MissionStatus.FAILED
