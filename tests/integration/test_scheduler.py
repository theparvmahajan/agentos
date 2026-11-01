import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from core import repository as repo
from core import scheduler
from core.db import get_conn
from core.models import MissionStatus, TaskStatus
from core.scheduler import run_scheduler, schedule_mission

AGENT_NAME = "scheduler-test-agent"


@pytest.fixture
def make_mission(test_db_url):
    """Committed missions with tasks, cleaned up afterwards (the scheduler opens its own connections)."""
    created = {"missions": [], "agent": None}

    def _make(spec, status=MissionStatus.RUNNING):
        # spec: [(key, [dependency keys])], dependencies listed first
        with get_conn(test_db_url) as conn:
            if created["agent"] is None:
                created["agent"] = repo.create_agent(conn, AGENT_NAME).id
            mission = repo.create_mission(conn, "scheduler test")
            ids = {}
            for key, deps in spec:
                task = repo.create_task(
                    conn, mission.id, key, created["agent"], f"do {key}",
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


# ---- helpers: look at the world, and play the part of a worker ----

def tasks_by_key(url, mission_id):
    with get_conn(url) as conn:
        return {t.key: t for t in repo.get_tasks(conn, mission_id)}


def queued_keys(url, mission_id, queues):
    """Keys of the tasks currently sitting on the task stream (without consuming them)."""
    by_id = {t.id: t.key for t in tasks_by_key(url, mission_id).values()}
    entries = queues.tasks.client.xrange(queues.tasks.name)
    return sorted(by_id[uuid.UUID(f["task_id"])] for _, f in entries)


def worker_runs(url, queues, mission_id, key, *, ok=True):
    """What a worker does: claim (READY -> RUNNING), finish, then announce the change."""
    task = tasks_by_key(url, mission_id)[key]
    with get_conn(url) as conn:
        repo.set_task_status(conn, task.id, TaskStatus.RUNNING)
        if ok:
            repo.set_task_status(conn, task.id, TaskStatus.COMPLETED, output=f"output of {key}")
        else:
            repo.set_task_status(conn, task.id, TaskStatus.FAILED, error="RuntimeError: boom")
    queues.publish_event(mission_id, task.id)


def mission(url, mission_id):
    with get_conn(url) as conn:
        return repo.get_mission(conn, mission_id)


def wait_until(condition, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


DIAMOND = [("a", []), ("b", ["a"]), ("c", ["a"]), ("d", ["b", "c"])]


# ---------- one scheduling pass ----------

def test_a_diamond_runs_in_waves_and_the_mission_completes(test_db_url, queues, make_mission):
    m = make_mission(DIAMOND)

    report = schedule_mission(m, queues, database_url=test_db_url)
    assert queued_keys(test_db_url, m, queues) == ["a"]  # only the root can start
    assert len(report.enqueued) == 1 and report.finished is False
    assert tasks_by_key(test_db_url, m)["a"].status == TaskStatus.READY

    worker_runs(test_db_url, queues, m, "a")
    schedule_mission(m, queues, database_url=test_db_url)
    assert queued_keys(test_db_url, m, queues) == ["a", "b", "c"]  # b and c go out together

    worker_runs(test_db_url, queues, m, "b")
    schedule_mission(m, queues, database_url=test_db_url)
    assert queued_keys(test_db_url, m, queues) == ["a", "b", "c"]  # d still waits for c

    worker_runs(test_db_url, queues, m, "c")
    schedule_mission(m, queues, database_url=test_db_url)
    assert queued_keys(test_db_url, m, queues) == ["a", "b", "c", "d"]

    worker_runs(test_db_url, queues, m, "d")
    report = schedule_mission(m, queues, database_url=test_db_url)

    assert report.finished is True
    done = mission(test_db_url, m)
    assert done.status == MissionStatus.COMPLETED
    assert done.result == "output of d"
    assert done.completed_at is not None


def test_scheduling_twice_without_progress_does_nothing_the_second_time(test_db_url, queues, make_mission):
    m = make_mission(DIAMOND)
    schedule_mission(m, queues, database_url=test_db_url)

    second = schedule_mission(m, queues, database_url=test_db_url)

    assert second.enqueued == () and second.skipped == ()
    assert queues.tasks.length() == 1  # task a was not enqueued twice


def test_a_failure_skips_dependents_but_independent_work_continues(test_db_url, queues, make_mission):
    m = make_mission([("a", []), ("b", ["a"]), ("c", ["b"]), ("d", ["a"])])
    schedule_mission(m, queues, database_url=test_db_url)
    worker_runs(test_db_url, queues, m, "a")
    schedule_mission(m, queues, database_url=test_db_url)  # enqueues b and d
    worker_runs(test_db_url, queues, m, "b", ok=False)

    report = schedule_mission(m, queues, database_url=test_db_url)

    tasks = tasks_by_key(test_db_url, m)
    assert tasks["c"].status == TaskStatus.SKIPPED
    assert len(report.skipped) == 1
    assert report.finished is False  # d is still queued

    worker_runs(test_db_url, queues, m, "d")
    schedule_mission(m, queues, database_url=test_db_url)

    done = mission(test_db_url, m)
    assert done.status == MissionStatus.FAILED
    assert "b: RuntimeError: boom" in done.error
    assert "skipped: c" in done.error
    assert tasks_by_key(test_db_url, m)["d"].status == TaskStatus.COMPLETED


def test_the_mission_finishes_as_soon_as_the_last_failure_settles(test_db_url, queues, make_mission):
    m = make_mission([("a", []), ("b", ["a"])])
    schedule_mission(m, queues, database_url=test_db_url)
    worker_runs(test_db_url, queues, m, "a", ok=False)

    report = schedule_mission(m, queues, database_url=test_db_url)

    assert report.finished is True
    assert mission(test_db_url, m).status == MissionStatus.FAILED
    assert tasks_by_key(test_db_url, m)["b"].status == TaskStatus.SKIPPED


def test_a_mission_with_no_tasks_fails(test_db_url, queues, make_mission):
    m = make_mission([])
    report = schedule_mission(m, queues, database_url=test_db_url)
    assert report.finished is True
    assert mission(test_db_url, m).error == "mission has no tasks"


@pytest.mark.parametrize("status", [MissionStatus.PENDING, MissionStatus.PLANNING, MissionStatus.COMPLETED])
def test_missions_that_are_not_running_are_left_alone(test_db_url, queues, make_mission, status):
    m = make_mission([("a", [])], status=status)
    report = schedule_mission(m, queues, database_url=test_db_url)
    assert report == scheduler.ScheduleReport()
    assert queues.tasks.length() == 0
    assert tasks_by_key(test_db_url, m)["a"].status == TaskStatus.PENDING


def test_an_unknown_mission_is_ignored(test_db_url, queues):
    assert schedule_mission(uuid.uuid4(), queues, database_url=test_db_url) == scheduler.ScheduleReport()


# ---------- races ----------

def test_many_schedulers_racing_enqueue_each_task_exactly_once(test_db_url, queues, make_mission):
    m = make_mission([("a", []), ("b", []), ("c", []), ("d", [])])  # four independent tasks

    with ThreadPoolExecutor(max_workers=8) as pool:
        reports = list(
            pool.map(lambda _: schedule_mission(m, queues, database_url=test_db_url), range(8))
        )

    assert queued_keys(test_db_url, m, queues) == ["a", "b", "c", "d"]  # no duplicates
    assert sum(len(r.enqueued) for r in reports) == 4


def test_mark_reports_a_lost_race(test_db_url, make_mission):
    m = make_mission([("a", [])])
    task = tasks_by_key(test_db_url, m)["a"]
    assert scheduler._mark(test_db_url, task.id, TaskStatus.READY) is True
    assert scheduler._mark(test_db_url, task.id, TaskStatus.READY) is False  # already moved


def test_two_schedulers_finishing_a_mission_only_finish_it_once(test_db_url, queues, make_mission):
    m = make_mission([("a", [])])
    schedule_mission(m, queues, database_url=test_db_url)
    worker_runs(test_db_url, queues, m, "a")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: schedule_mission(m, queues, database_url=test_db_url), range(6)))

    done = mission(test_db_url, m)
    assert done.status == MissionStatus.COMPLETED and done.result == "output of a"


# ---------- the scheduler service loop ----------

@pytest.fixture
def running_scheduler(test_db_url, queues):
    stop = threading.Event()
    thread = threading.Thread(
        target=run_scheduler,
        args=(queues,),
        kwargs={"consumer": "test-scheduler", "stop": stop, "database_url": test_db_url,
                "block_ms": 100, "retry_delay": 0.05},
        daemon=True,
    )
    thread.start()
    yield
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_the_loop_drives_a_mission_from_event_to_completion(test_db_url, queues, make_mission, running_scheduler):
    m = make_mission([("a", []), ("b", ["a"])])

    queues.publish_event(m)  # e.g. "the planner just finished planning this mission"
    assert wait_until(lambda: queued_keys(test_db_url, m, queues) == ["a"])

    worker_runs(test_db_url, queues, m, "a")
    assert wait_until(lambda: queued_keys(test_db_url, m, queues) == ["a", "b"])

    worker_runs(test_db_url, queues, m, "b")
    assert wait_until(lambda: mission(test_db_url, m).status == MissionStatus.COMPLETED)

    assert wait_until(lambda: queues.events.pending_count() == 0 and queues.events.length() == 0)


def test_many_events_for_one_mission_cause_one_enqueue(test_db_url, queues, make_mission, running_scheduler):
    m = make_mission([("a", [])])
    for _ in range(5):
        queues.publish_event(m)

    assert wait_until(lambda: queues.events.length() == 0)
    assert queued_keys(test_db_url, m, queues) == ["a"]


def test_malformed_events_are_dropped_not_retried_forever(test_db_url, queues, running_scheduler):
    queues.events.publish({"nonsense": "yes"})
    queues.events.publish({"mission_id": "not-a-uuid"})

    assert wait_until(lambda: queues.events.length() == 0 and queues.events.pending_count() == 0)


def test_a_failed_pass_is_retried_until_it_succeeds(test_db_url, queues, make_mission, running_scheduler, monkeypatch):
    m = make_mission([("a", [])])
    real = scheduler.schedule_mission
    attempts = []

    def flaky(mission_id, q, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("database hiccup")
        return real(mission_id, q, **kwargs)

    monkeypatch.setattr(scheduler, "schedule_mission", flaky)
    queues.publish_event(m)

    assert wait_until(lambda: queued_keys(test_db_url, m, queues) == ["a"])
    assert len(attempts) == 3  # failed twice, then the event was replayed and succeeded
    assert wait_until(lambda: queues.events.pending_count() == 0)