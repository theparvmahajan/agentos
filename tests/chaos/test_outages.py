"""Failure injection: Redis or Postgres goes away in the middle of a mission.

Each test states what MUST happen. A ChaosProxy sits between the components and the real server
(see tests/chaos/proxy.py), so the outage is a real broken connection, not a mocked exception.
"""

import sys
import threading
import time
import traceback

import pytest
import redis

from core import repository as repo
from core.db import get_conn
from core.models import MissionStatus, TaskStatus
from core.queue import Stream
from core.retry import Backoff
from core.scheduler import run_scheduler
from core.tools import ToolRegistry
from core.worker import run_worker
from tests.chaos.conftest import fresh_prefix, proxied_queues
from tests.chaos.proxy import BLACKHOLE, RESET, UP, through
from tests.integration.conftest import LEASE_AGENT, wait_until

FAST = Backoff(0.05, 0.1)
OUTAGE_SECONDS = 2.5


def start_system(queues, db_url, runner, lease=2.0):
    """A scheduler and a worker as threads. Returns a function that stops them."""
    stop = threading.Event()
    threads = [
        threading.Thread(target=run_scheduler, daemon=True, kwargs={
            "queues": queues, "consumer": "s", "stop": stop, "database_url": db_url,
            "block_ms": 100, "retry_delay": 0.2, "reap_interval": 0.3, "lease_seconds": lease,
            "sweep_interval": 0.2, "backoff": FAST}),
        threading.Thread(target=run_worker, daemon=True, kwargs={
            "queues": queues, "consumer": "w", "stop": stop, "database_url": db_url,
            "runners": {LEASE_AGENT: runner}, "tools": ToolRegistry(), "block_ms": 100,
            "retry_delay": 0.2, "lease_seconds": lease, "heartbeat_seconds": 0.5, "backoff": FAST}),
    ]
    for t in threads:
        t.start()

    def shutdown():
        stop.set()
        for t in threads:
            t.join(20)
        hung = [t for t in threads if t.is_alive()]
        if hung:  # show where each one is stuck: the stack is the diagnosis
            frames = sys._current_frames()
            report = "\n".join(
                f"--- {t.name}:\n" + "".join(traceback.format_stack(frames[t.ident])[-8:])
                for t in hung
            )
            raise AssertionError(f"a component hung and did not stop:\n{report}")

    return shutdown, threads


def mission_status(url, mission_id):
    with get_conn(url) as conn:
        return repo.get_mission(conn, mission_id).status


def tasks_of(url, mission_id):
    with get_conn(url) as conn:
        return repo.get_tasks(conn, mission_id)


# ---------- an outage in the middle of a mission ----------

@pytest.mark.parametrize("fault", [
    ("redis", RESET),       # the Redis server crashed or restarted
    ("redis", BLACKHOLE),   # a network partition: no error, no answer
    ("postgres", RESET),    # the database connection was cut
])
def test_a_mission_survives_an_outage_and_every_task_completes(
    test_db_url, chain, counting_runner, redis_proxy, db_proxy, redis_cleanup, fault
):
    target, mode = fault
    mission_id, _ = chain(4)
    prefix = fresh_prefix()
    redis_cleanup.append(prefix)
    queues = proxied_queues(redis_proxy, prefix)
    db_url = through(test_db_url, db_proxy)
    runner = counting_runner(delay=0.4)
    shutdown, _ = start_system(queues, db_url, runner)
    try:
        queues.publish_event(mission_id)
        assert wait_until(  # the mission is under way: the first step has started
            lambda: tasks_of(test_db_url, mission_id)[0].status != TaskStatus.PENDING, 10)

        proxy = redis_proxy if target == "redis" else db_proxy
        proxy.set_mode(mode)
        time.sleep(OUTAGE_SECONDS)
        proxy.set_mode(UP)

        assert wait_until(
            lambda: mission_status(test_db_url, mission_id) == MissionStatus.COMPLETED, 45
        ), [(t.key, t.status.value, t.attempts) for t in tasks_of(test_db_url, mission_id)]
    finally:
        shutdown()

    tasks = tasks_of(test_db_url, mission_id)
    assert all(t.status == TaskStatus.COMPLETED for t in tasks)
    if target == "redis":
        # Redis only carries nudges: no task had to be run twice because of it.
        assert [t.attempts for t in tasks] == [1, 1, 1, 1]
        assert all(count == 1 for count in counting_runner.calls.values())
    else:
        assert all(t.attempts <= 2 for t in tasks)  # at worst one wasted attempt on the step hit


def test_an_outage_that_hits_the_result_save_costs_one_extra_attempt_and_nothing_more(
    test_db_url, chain, redis_proxy, db_proxy, redis_cleanup
):
    """The nastiest moment for Postgres to disappear: the work is done and the result is about to
    be saved. The worker cannot save it, its lease then lapses, the reaper hands the task to a
    new attempt, and the mission still completes. Cost: one repeated step."""
    mission_id, _ = chain(2)
    prefix = fresh_prefix()
    redis_cleanup.append(prefix)
    queues = proxied_queues(redis_proxy, prefix)
    calls = []

    def cuts_the_database_just_before_returning(agent, instruction, dependencies, *, tools, provider=None):
        calls.append(instruction)
        time.sleep(0.3)
        if len(calls) == 1:
            db_proxy.set_mode(RESET)
            threading.Timer(1.5, lambda: db_proxy.set_mode(UP)).start()
        from core.runner import AgentResult
        return AgentResult(output=f"{instruction} done")

    shutdown, _ = start_system(queues, through(test_db_url, db_proxy), cuts_the_database_just_before_returning)
    try:
        queues.publish_event(mission_id)
        assert wait_until(
            lambda: mission_status(test_db_url, mission_id) == MissionStatus.COMPLETED, 45
        ), [(t.key, t.status.value, t.attempts) for t in tasks_of(test_db_url, mission_id)]
    finally:
        shutdown()

    first, second = tasks_of(test_db_url, mission_id)
    assert (first.attempts, first.status) == (2, TaskStatus.COMPLETED)  # one wasted attempt
    assert "lease expired" in first.last_error
    assert (second.attempts, second.status) == (1, TaskStatus.COMPLETED)
    assert calls.count("step 0") == 2 and calls.count("step 1") == 1


# ---------- a failure at one precise moment ----------

def test_a_failed_acknowledgement_does_not_kill_the_scheduler(
    test_db_url, queues, chain, monkeypatch
):
    """Redis blips exactly when the scheduler acknowledges an event. Before this was fixed the
    exception ended the scheduler loop: it would quietly stop scheduling anything, ever."""
    first_mission, _ = chain(1)
    second_mission, _ = chain(1)
    real_ack = Stream.ack
    failures = []

    def ack_that_fails_once(self, message_id):
        if not failures:
            failures.append(message_id)
            raise redis.ConnectionError("Redis went away during XACK")
        return real_ack(self, message_id)

    monkeypatch.setattr(Stream, "ack", ack_that_fails_once)
    stop = threading.Event()
    scheduler = threading.Thread(target=run_scheduler, daemon=True, kwargs={
        "queues": queues, "consumer": "s", "stop": stop, "database_url": test_db_url,
        "block_ms": 100, "retry_delay": 0.1})
    scheduler.start()
    try:
        queues.publish_event(first_mission)
        assert wait_until(lambda: queues.tasks.length() == 1, 10)  # pass 1 ran...
        # ...and its acknowledgement fails a moment AFTER the task is published, so wait for it
        # (asserting at once passed on a quiet machine and failed on a loaded one)
        assert wait_until(lambda: bool(failures), 10)
        assert scheduler.is_alive()

        queues.publish_event(second_mission)  # the loop must still be working
        assert wait_until(lambda: queues.tasks.length() == 2, 10)
        assert wait_until(lambda: queues.events.pending_count() == 0, 10)  # and acks recover
    finally:
        stop.set()
        scheduler.join(10)
