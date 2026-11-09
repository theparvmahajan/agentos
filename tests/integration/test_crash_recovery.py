"""What happens when a worker dies, hangs or comes back late. Real Postgres and Redis; the most
important test starts a real worker process and kills it."""

import os
import subprocess
import sys
import threading
import time

from core import repository as repo
from core.db import get_conn
from core.models import MissionStatus, TaskStatus
from core.reaper import reap_expired
from core.retry import Backoff
from core.runner import AgentResult
from core.scheduler import run_scheduler, schedule_mission
from core.sweeper import sweep_ready
from core.tools import ToolRegistry
from core.worker import Outcome, handle_task_message, run_worker
from tests.integration.conftest import LEASE_AGENT, make_due, wait_until


def task_of(url, task_id):
    with get_conn(url) as conn:
        return repo.get_task(conn, task_id)


def mission_status(url, mission_id):
    with get_conn(url) as conn:
        return repo.get_mission(conn, mission_id).status


def runner_returning(text, delay=0.0):
    def run(agent, instruction, dependencies, *, tools, provider=None):
        time.sleep(delay)
        return AgentResult(output=text)

    return run


def handle(url, queues, message, runner, **kwargs):
    return handle_task_message(
        message, queues, runners={LEASE_AGENT: runner}, tools=ToolRegistry(), database_url=url, **kwargs
    )


def start_thread(target, **kwargs):
    stop = threading.Event()
    thread = threading.Thread(target=target, kwargs={"stop": stop, **kwargs}, daemon=True)
    thread.start()
    return stop, thread


# ---------- the heartbeat keeps a slow, healthy task alive ----------

def test_a_slow_task_keeps_its_lease_and_is_never_reaped(test_db_url, queues, lease_mission):
    mission_id, ids = lease_mission()
    queues.publish_task(mission_id, ids["a"])
    [message] = queues.tasks.read("w")
    reaped = []
    stop_reaping = threading.Event()

    def keep_reaping():
        while not stop_reaping.is_set():
            report = reap_expired(queues, lease_seconds=0.6, database_url=test_db_url)
            reaped.extend(report.requeued + report.failed)
            time.sleep(0.05)

    reaper = threading.Thread(target=keep_reaping, daemon=True)
    reaper.start()
    try:
        # Runs 1.8 s on a 0.6 s lease: it survives only because the heartbeat renews it.
        outcome = handle(
            test_db_url, queues, message, runner_returning("slow but alive", delay=1.8),
            consumer="w", lease_seconds=0.6, heartbeat_seconds=0.15,
        )
    finally:
        stop_reaping.set()
        reaper.join(5)

    task = task_of(test_db_url, ids["a"])
    assert outcome == Outcome.COMPLETED and task.attempts == 1 and task.output == "slow but alive"
    assert reaped == []


def test_without_a_heartbeat_the_same_slow_task_is_taken_away(test_db_url, queues, lease_mission):
    """The control for the test above: it is the heartbeat, not luck, that keeps the task."""
    mission_id, ids = lease_mission()
    queues.publish_task(mission_id, ids["a"])
    [message] = queues.tasks.read("w")
    outcomes = []
    worker_thread = threading.Thread(
        target=lambda: outcomes.append(handle(
            test_db_url, queues, message, runner_returning("late", delay=1.5),
            consumer="w", lease_seconds=0.5, heartbeat_seconds=60,  # effectively never beats
        )),
        daemon=True,
    )
    worker_thread.start()

    assert wait_until(lambda: task_of(test_db_url, ids["a"]).status == TaskStatus.RUNNING)
    time.sleep(0.7)  # past the lease
    assert reap_expired(queues, lease_seconds=0.5, database_url=test_db_url).requeued == (ids["a"],)
    worker_thread.join(10)
    assert outcomes == [Outcome.DISCARDED]


# ---------- a zombie comes back after the task was handed on ----------

def test_a_worker_that_wakes_up_late_cannot_overwrite_the_retry(test_db_url, queues, lease_mission):
    mission_id, ids = lease_mission()
    queues.publish_task(mission_id, ids["a"])
    [first] = queues.tasks.read("zombie")
    gate = threading.Event()

    def zombie_runner(agent, instruction, dependencies, *, tools, provider=None):
        gate.wait(10)  # frozen (a long pause, a stuck network call) while its lease expires
        return AgentResult(output="from the zombie")

    results = {}
    zombie = threading.Thread(
        target=lambda: results.update(zombie=handle(
            test_db_url, queues, first, zombie_runner,
            consumer="zombie", lease_seconds=0.5, heartbeat_seconds=60,
        )),
        daemon=True,
    )
    zombie.start()
    assert wait_until(lambda: task_of(test_db_url, ids["a"]).status == TaskStatus.RUNNING)
    time.sleep(0.7)

    assert reap_expired(queues, lease_seconds=0.5, database_url=test_db_url).requeued == (ids["a"],)
    make_due(test_db_url, ids["a"])
    sweep_ready(queues, database_url=test_db_url)
    [second] = queues.tasks.read("healthy")
    assert handle(
        test_db_url, queues, second, runner_returning("from the healthy worker"), consumer="healthy"
    ) == Outcome.COMPLETED

    gate.set()  # the zombie wakes up and tries to save its (stale) result
    zombie.join(10)

    task = task_of(test_db_url, ids["a"])
    assert results["zombie"] == Outcome.DISCARDED
    assert task.output == "from the healthy worker" and task.attempts == 2
    assert task.status == TaskStatus.COMPLETED


def test_a_zombie_that_finishes_while_the_retry_is_still_running_is_refused(
    test_db_url, queues, lease_mission
):
    """The dangerous interleaving. Without the fencing token, the zombie's late result would be
    accepted here (the task IS running, just on a newer attempt) and the retry's work, still in
    progress, would be silently replaced."""
    mission_id, ids = lease_mission()
    queues.publish_task(mission_id, ids["a"])
    [first] = queues.tasks.read("zombie")
    zombie_gate, healthy_gate = threading.Event(), threading.Event()

    def gated(gate, text):
        def run(agent, instruction, dependencies, *, tools, provider=None):
            gate.wait(10)
            return AgentResult(output=text)

        return run

    results = {}
    zombie = threading.Thread(
        target=lambda: results.update(zombie=handle(
            test_db_url, queues, first, gated(zombie_gate, "from the zombie"),
            consumer="zombie", lease_seconds=0.5, heartbeat_seconds=60,
        )),
        daemon=True,
    )
    zombie.start()
    assert wait_until(lambda: task_of(test_db_url, ids["a"]).status == TaskStatus.RUNNING)
    time.sleep(0.7)
    assert reap_expired(queues, lease_seconds=0.5, database_url=test_db_url).requeued == (ids["a"],)
    make_due(test_db_url, ids["a"])
    sweep_ready(queues, database_url=test_db_url)
    [second] = queues.tasks.read("healthy")
    healthy = threading.Thread(
        target=lambda: results.update(healthy=handle(
            test_db_url, queues, second, gated(healthy_gate, "from the healthy worker"),
            consumer="healthy", lease_seconds=30, heartbeat_seconds=10,
        )),
        daemon=True,
    )
    healthy.start()
    assert wait_until(lambda: task_of(test_db_url, ids["a"]).attempts == 2)

    zombie_gate.set()  # the zombie finishes FIRST, while attempt 2 is still running
    zombie.join(10)
    mid = task_of(test_db_url, ids["a"])
    assert results["zombie"] == Outcome.DISCARDED
    assert mid.status == TaskStatus.RUNNING and mid.output is None and mid.attempts == 2

    healthy_gate.set()
    healthy.join(10)
    done = task_of(test_db_url, ids["a"])
    assert results["healthy"] == Outcome.COMPLETED
    assert done.output == "from the healthy worker" and done.status == TaskStatus.COMPLETED


def test_a_task_that_keeps_losing_its_worker_ends_up_failed_not_looping_forever(
    test_db_url, queues, lease_mission
):
    mission_id, ids = lease_mission(max_attempts=2)
    for expected_attempt in (1, 2):
        with get_conn(test_db_url) as conn:  # a worker claims it and dies at once
            make_due(test_db_url, ids["a"])  # (a no-op the first time: no retry is waiting yet)
            repo.claim_task(conn, ids["a"], "doomed", 30)
            conn.execute("UPDATE task_leases SET expires_at = now() - interval '1 s'")
        report = reap_expired(queues, lease_seconds=30, database_url=test_db_url)
        assert task_of(test_db_url, ids["a"]).attempts == expected_attempt
        assert (report.requeued if expected_attempt == 1 else report.failed) == (ids["a"],)

    assert task_of(test_db_url, ids["a"]).status == TaskStatus.FAILED
    schedule_mission(mission_id, queues, database_url=test_db_url)
    assert mission_status(test_db_url, mission_id) == MissionStatus.FAILED


# ---------- the headline: kill -9 a real worker process ----------

def test_killing_a_worker_process_mid_task_does_not_lose_the_mission(
    test_db_url, queues, lease_mission
):
    """Before leases, this left the task 'running' forever and the mission never finished.
    Now: the lease runs out, the scheduler's reaper requeues the task, another worker finishes it."""
    mission_id, ids = lease_mission(keys=("a",), ready=False)  # pending: the scheduler queues it
    with get_conn(test_db_url) as conn:  # b waits for a, so the mission can only finish if a does
        agent_id = repo.get_task(conn, ids["a"]).agent_id
        b = repo.create_task(conn, mission_id, "b", agent_id, "do b", depends_on=[ids["a"]])
    prefix = queues.tasks.name.rsplit(":", 1)[0]
    env = {**os.environ, "DATABASE_URL": test_db_url}

    stops = []
    scheduler_stop, scheduler = start_thread(
        run_scheduler, queues=queues, consumer="sched", database_url=test_db_url,
        block_ms=100, reap_interval=0.3, lease_seconds=1.5,
        sweep_interval=0.2, backoff=Backoff(0.05, 0.1),  # retries wait milliseconds, not seconds
    )
    stops.append((scheduler_stop, scheduler))
    child = subprocess.Popen(
        [sys.executable, "-m", "tests.integration.crash_child", prefix, LEASE_AGENT, test_db_url],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        queues.publish_event(mission_id)  # wake the scheduler: it queues task a
        assert wait_until(lambda: task_of(test_db_url, ids["a"]).status == TaskStatus.RUNNING, 20)
        first_lease = None
        with get_conn(test_db_url) as conn:
            first_lease = conn.execute(
                "SELECT owner FROM task_leases WHERE task_id = %s", (ids["a"],)
            ).fetchone()
        assert first_lease["owner"] == "doomed"

        child.kill()  # SIGKILL on Linux/macOS, TerminateProcess on Windows: no cleanup runs
        child.wait(10)

        survivor_stop, survivor = start_thread(
            run_worker, queues=queues, consumer="survivor", runners={LEASE_AGENT: runner_returning("done")},
            tools=ToolRegistry(), database_url=test_db_url, block_ms=100,
            lease_seconds=1.5, heartbeat_seconds=0.4,
        )
        stops.append((survivor_stop, survivor))

        assert wait_until(lambda: mission_status(test_db_url, mission_id) == MissionStatus.COMPLETED, 30)
    finally:
        if child.poll() is None:
            child.kill()
        for stop, _ in stops:
            stop.set()
        for _, thread in stops:
            thread.join(5)

    a, b_done = task_of(test_db_url, ids["a"]), task_of(test_db_url, b.id)
    assert a.status == TaskStatus.COMPLETED and a.attempts == 2  # first attempt died, second finished
    assert "lease expired" in a.last_error
    assert b_done.status == TaskStatus.COMPLETED
    # Known gap, asserted so that fixing it is a deliberate change: the message the dead worker
    # had received is still owned by it (a different consumer name), so it stays pending until
    # XAUTOCLAIM is added. It is harmless: the task was recovered through the reaper instead.
    assert queues.tasks.pending_count() == 1
