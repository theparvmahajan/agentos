"""Failure injection: processes are killed outright in the middle of a mission.

The scheduler and the worker run as REAL processes and are killed with Popen.kill()
(SIGKILL on Linux/macOS, TerminateProcess on Windows): no cleanup code gets to run.
A replacement is started afterwards and the mission must still finish.
"""

import os
import subprocess
import sys
import time

import pytest

from core import repository as repo
from core.db import get_conn
from core.models import MissionStatus, TaskStatus
from core.queue import build_queues
from tests.chaos.conftest import fresh_prefix
from tests.integration.conftest import LEASE_AGENT, wait_until

STEP_SECONDS = 0.7


def _output(name):
    """CHAOS_LOGDIR=some/dir keeps each child's log (otherwise they are discarded)."""
    logdir = os.environ.get("CHAOS_LOGDIR")
    if not logdir:
        return subprocess.DEVNULL
    os.makedirs(logdir, exist_ok=True)
    return open(os.path.join(logdir, f"{name}-{time.time_ns()}.log"), "w")


def spawn_scheduler(prefix, db_url):
    env = {
        **os.environ,
        "DATABASE_URL": db_url, "QUEUE_PREFIX": prefix, "SCHEDULER_NAME": "chaos-scheduler",
        "TASK_LEASE_SECONDS": "2", "LEASE_HEARTBEAT_SECONDS": "0.5", "REAPER_INTERVAL_SECONDS": "0.5",
        "SWEEP_INTERVAL_SECONDS": "0.2", "RETRY_BASE_SECONDS": "0.05", "RETRY_MAX_SECONDS": "0.1",
    }
    out = _output("scheduler")
    return subprocess.Popen([sys.executable, "-m", "services.scheduler"], env=env, stdout=out, stderr=out)


def spawn_worker(prefix, db_url):
    out = _output("worker")
    return subprocess.Popen(
        [sys.executable, "-m", "tests.chaos.child_worker", prefix, LEASE_AGENT, db_url, str(STEP_SECONDS)],
        env={**os.environ, "DATABASE_URL": db_url}, stdout=out, stderr=out,
    )


def diagnostics(url, mission_id, queues, procs):
    """Everything needed to understand a stuck mission, for the failure message."""
    with get_conn(url) as conn:
        rows = conn.execute(
            """
            SELECT t.key, t.status, t.attempts, l.owner, l.attempt AS lease_attempt,
                   round(extract(epoch FROM l.expires_at - now())::numeric, 1) AS lease_left,
                   round(extract(epoch FROM d.run_after - now())::numeric, 1) AS due_in,
                   round(extract(epoch FROM now() - d.enqueued_at)::numeric, 1) AS enqueued_ago,
                   round(extract(epoch FROM now() - t.updated_at)::numeric, 1) AS idle
            FROM tasks t
            LEFT JOIN task_leases l ON l.task_id = t.id
            LEFT JOIN task_dispatch d ON d.task_id = t.id
            WHERE t.mission_id = %s ORDER BY t.key
            """,
            (mission_id,),
        ).fetchall()
    alive = {name: proc.poll() is None for name, proc in procs.items()}
    return (
        f"tasks={[dict(r) for r in rows]}\n"
        f"tasks stream: length={queues.tasks.length()} pending={queues.tasks.pending_count()}; "
        f"events stream: length={queues.events.length()} pending={queues.events.pending_count()}; "
        f"processes alive={alive}"
    )


def tasks_of(url, mission_id):
    with get_conn(url) as conn:
        return repo.get_tasks(conn, mission_id)


def mission_status(url, mission_id):
    with get_conn(url) as conn:
        return repo.get_mission(conn, mission_id).status


@pytest.mark.parametrize("victims", [("scheduler",), ("worker",), ("scheduler", "worker")])
def test_killing_the_scheduler_or_the_worker_or_both_does_not_lose_the_mission(
    test_db_url, chain, redis_client, victims
):
    mission_id, _ = chain(5)
    prefix = fresh_prefix()
    build_queues(redis_client, prefix=prefix).ensure_groups()
    queues = build_queues(redis_client, prefix=prefix)
    procs = {"scheduler": spawn_scheduler(prefix, test_db_url), "worker": spawn_worker(prefix, test_db_url)}
    try:
        queues.publish_event(mission_id)
        assert wait_until(  # under way: at least one step is finished and another is in progress
            lambda: any(t.status == TaskStatus.COMPLETED for t in tasks_of(test_db_url, mission_id)), 30)

        for name in victims:
            procs[name].kill()
        for name in victims:
            procs[name].wait(10)
        time.sleep(1.0)  # the system is down: nothing runs, nothing is announced

        for name in victims:  # replacements, same names, so they pick up unfinished messages
            procs[name] = (spawn_scheduler if name == "scheduler" else spawn_worker)(prefix, test_db_url)

        assert wait_until(
            lambda: mission_status(test_db_url, mission_id) == MissionStatus.COMPLETED, 60
        ), diagnostics(test_db_url, mission_id, queues, procs)
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.kill()
        for proc in procs.values():
            proc.wait(10)
        redis_client.delete(f"{prefix}:tasks", f"{prefix}:events")

    tasks = tasks_of(test_db_url, mission_id)
    assert all(t.status == TaskStatus.COMPLETED for t in tasks)
    assert all(t.attempts <= 2 for t in tasks)  # at most one repeated step, and only if the worker died
    if victims == ("scheduler",):
        assert all(t.attempts == 1 for t in tasks)  # a dead scheduler never causes repeated work
