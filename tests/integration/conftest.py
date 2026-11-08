import time

import pytest

from core import repository as repo
from core.db import get_conn
from core.models import MissionStatus, TaskStatus

LEASE_AGENT = "lease-test-agent"


def wait_until(condition, timeout=10.0):
    """Poll instead of sleeping a fixed time."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def lease_mission(test_db_url):
    """Make a RUNNING mission with independent tasks. By default they are already READY (as the
    scheduler leaves them); ready=False leaves them PENDING so the real scheduler can pick them up.

    make(["a", "b"]) creates two tasks; max_attempts applies to all of them.
    Returns (mission_id, {key: task_id}).
    """
    created = {"missions": [], "agent": None}

    def _make(keys=("a",), max_attempts=3, ready=True):
        with get_conn(test_db_url) as conn:
            if created["agent"] is None:
                created["agent"] = repo.create_agent(conn, LEASE_AGENT, capabilities=[]).id
            mission = repo.create_mission(conn, "lease test")
            ids = {}
            for key in keys:
                task = repo.create_task(
                    conn, mission.id, key, created["agent"], f"do {key}", max_attempts=max_attempts
                )
                if ready:
                    repo.set_task_status(conn, task.id, TaskStatus.READY)
                ids[key] = task.id
            repo.set_mission_status(conn, mission.id, MissionStatus.RUNNING)
        created["missions"].append(mission.id)
        return mission.id, ids

    yield _make

    with get_conn(test_db_url) as conn:
        for mission_id in created["missions"]:
            conn.execute("DELETE FROM missions WHERE id = %s", (mission_id,))
        if created["agent"] is not None:
            conn.execute("DELETE FROM agents WHERE id = %s", (created["agent"],))
