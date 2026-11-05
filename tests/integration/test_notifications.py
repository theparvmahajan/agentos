"""The database trigger and PgListener, against real Postgres."""

import threading
import time
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from core import repository as repo
from core.db import get_conn
from core.events import CHANNEL, PgListener
from core.models import TaskStatus

MARK = "[notify-test] "


@pytest.fixture
def agent_id(test_db_url):
    with get_conn(test_db_url) as conn:
        agent = repo.create_agent(conn, f"notify-agent-{uuid.uuid4().hex[:6]}")
    yield agent.id
    with get_conn(test_db_url) as conn:
        conn.execute("DELETE FROM missions WHERE objective LIKE %s", (MARK + "%",))
        conn.execute("DELETE FROM agents WHERE id = %s", (agent.id,))


@pytest.fixture
def listen(test_db_url):
    """Start a listener that appends every notice to a list."""
    started: list[PgListener] = []

    def start(*, on_notice=None, on_reconnect=None, name=None):
        notices: list = []
        listener = PgListener(
            on_notice or notices.append,
            on_reconnect=on_reconnect,
            database_url=test_db_url,
            application_name=name or f"test-listener-{uuid.uuid4().hex[:8]}",
            reconnect_delay=0.1,
        )
        listener.start()
        assert listener.listening.wait(5), "listener did not connect"
        started.append(listener)
        return listener, notices

    yield start
    for listener in started:
        listener.stop()
    for listener in started:
        listener.join(3)
        assert not listener.is_alive()


def wait_until(condition, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def new_mission(test_db_url, agent_id, with_task=True):
    with get_conn(test_db_url) as conn:
        mission = repo.create_mission(conn, MARK + "x")
        task = repo.create_task(conn, mission.id, "t1", agent_id, "work") if with_task else None
    return mission, task


def test_every_committed_change_is_announced_with_ids_and_a_status(test_db_url, agent_id, listen):
    _, notices = listen()
    mission, task = new_mission(test_db_url, agent_id)
    with get_conn(test_db_url) as conn:
        repo.set_task_status(conn, task.id, TaskStatus.READY)

    mine = lambda: [n for n in notices if n.mission_id == mission.id]
    assert wait_until(lambda: len(mine()) >= 3)
    assert [(n.kind, n.status) for n in mine()[:3]] == [
        ("mission", "pending"),
        ("task", "pending"),
        ("task", "ready"),
    ]
    assert mine()[1].task_id == task.id and mine()[0].task_id is None


def test_a_rolled_back_change_is_never_announced(test_db_url, agent_id, listen):
    _, notices = listen()
    with psycopg.connect(test_db_url, row_factory=dict_row) as c:
        ghost = repo.create_mission(c, MARK + "never committed")
        c.rollback()
    real, _ = new_mission(test_db_url, agent_id, with_task=False)

    assert wait_until(lambda: any(n.mission_id == real.id for n in notices))
    assert not any(n.mission_id == ghost.id for n in notices)


def test_a_huge_task_output_does_not_break_the_write_or_the_announcement(
    test_db_url, agent_id, listen
):
    """Postgres rejects a NOTIFY payload over 8000 bytes, which would abort the write. The
    trigger therefore sends ids only; this proves a 300 KB result still saves and announces."""
    _, notices = listen()
    _, task = new_mission(test_db_url, agent_id)
    huge = "x" * 300_000
    with get_conn(test_db_url) as conn:
        repo.set_task_status(conn, task.id, TaskStatus.READY)
        repo.set_task_status(conn, task.id, TaskStatus.RUNNING)
        repo.set_task_status(conn, task.id, TaskStatus.COMPLETED, output=huge)

    assert wait_until(lambda: any(n.status == "completed" and n.kind == "task" for n in notices))
    with get_conn(test_db_url) as conn:
        assert len(repo.get_task(conn, task.id).output) == 300_000


def test_the_listener_reconnects_after_its_connection_is_killed_and_asks_to_resync(
    test_db_url, agent_id, listen
):
    reconnected = threading.Event()
    name = f"killme-{uuid.uuid4().hex[:8]}"
    listener, notices = listen(on_reconnect=reconnected.set, name=name)

    with psycopg.connect(test_db_url, autocommit=True) as admin:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = %s",
            (name,),
        )
    assert reconnected.wait(5), "on_reconnect was not called"
    assert listener.listening.wait(5)

    mission, _ = new_mission(test_db_url, agent_id, with_task=False)  # after the reconnect
    assert wait_until(lambda: any(n.mission_id == mission.id for n in notices))


def test_a_callback_that_raises_does_not_kill_the_listener(test_db_url, agent_id, listen):
    seen: list = []

    def flaky(notice):
        seen.append(notice)
        if len(seen) == 1:
            raise RuntimeError("callback bug")

    listener, _ = listen(on_notice=flaky)
    first, _ = new_mission(test_db_url, agent_id, with_task=False)
    second, _ = new_mission(test_db_url, agent_id, with_task=False)

    assert wait_until(lambda: {first.id, second.id} <= {n.mission_id for n in seen})
    assert listener.is_alive()


def test_a_malformed_notification_is_ignored(test_db_url, agent_id, listen):
    listener, notices = listen()
    with psycopg.connect(test_db_url, autocommit=True) as c:
        c.execute("SELECT pg_notify(%s, 'this is not json')", (CHANNEL,))
    real, _ = new_mission(test_db_url, agent_id, with_task=False)

    assert wait_until(lambda: any(n.mission_id == real.id for n in notices))
    assert listener.is_alive()


def test_stop_returns_promptly(test_db_url, listen):
    listener, _ = listen()
    started = time.time()
    listener.stop()
    listener.join(3)
    assert not listener.is_alive() and time.time() - started < 2
