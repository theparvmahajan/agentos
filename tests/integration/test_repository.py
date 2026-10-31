import pytest

from core import repository as repo
from core.models import MissionStatus, TaskStatus
from core.state import InvalidTransition


def new_agent(conn):
    return repo.create_agent(conn, "researcher", capabilities=["web.search"])


def test_create_and_get_agent(conn):
    agent = new_agent(conn)
    fetched = repo.get_agent_by_name(conn, "researcher")
    assert fetched == agent
    assert fetched.capabilities == ["web.search"]


def test_get_missing_agent_returns_none(conn):
    assert repo.get_agent_by_name(conn, "nope") is None


def test_create_mission_defaults_to_pending(conn):
    mission = repo.create_mission(conn, "analyze sales")
    assert mission.status == MissionStatus.PENDING
    assert repo.get_mission(conn, mission.id) == mission


def test_mission_completion_sets_timestamp(conn):
    mission = repo.create_mission(conn, "x")
    done = repo.set_mission_status(
        conn, mission.id, MissionStatus.COMPLETED, result="report"
    )
    assert done.completed_at is not None
    assert done.result == "report"


def test_dependencies_round_trip(conn):
    agent = new_agent(conn)
    mission = repo.create_mission(conn, "x")
    t1 = repo.create_task(conn, mission.id, "t1", agent.id, "first")
    repo.create_task(conn, mission.id, "t2", agent.id, "second", depends_on=[t1.id])

    tasks = {t.key: t for t in repo.get_tasks(conn, mission.id)}
    assert tasks["t1"].depends_on == []
    assert tasks["t2"].depends_on == [t1.id]


def test_dependency_must_be_in_same_mission(conn):
    agent = new_agent(conn)
    m1 = repo.create_mission(conn, "one")
    m2 = repo.create_mission(conn, "two")
    other = repo.create_task(conn, m1.id, "t1", agent.id, "x")
    with pytest.raises(ValueError):
        repo.create_task(conn, m2.id, "t1", agent.id, "y", depends_on=[other.id])


def test_task_happy_path(conn):
    agent = new_agent(conn)
    mission = repo.create_mission(conn, "x")
    task = repo.create_task(conn, mission.id, "t1", agent.id, "do it")

    task = repo.set_task_status(conn, task.id, TaskStatus.READY)
    task = repo.set_task_status(conn, task.id, TaskStatus.RUNNING)
    assert task.attempts == 1
    assert task.started_at is not None

    task = repo.set_task_status(conn, task.id, TaskStatus.COMPLETED, output="done")
    assert task.output == "done"
    assert task.finished_at is not None


def test_cannot_skip_states(conn):
    agent = new_agent(conn)
    mission = repo.create_mission(conn, "x")
    task = repo.create_task(conn, mission.id, "t1", agent.id, "do it")
    with pytest.raises(InvalidTransition):
        repo.set_task_status(conn, task.id, TaskStatus.COMPLETED)


def test_terminal_state_is_final(conn):
    agent = new_agent(conn)
    mission = repo.create_mission(conn, "x")
    task = repo.create_task(conn, mission.id, "t1", agent.id, "do it")
    repo.set_task_status(conn, task.id, TaskStatus.READY)
    repo.set_task_status(conn, task.id, TaskStatus.RUNNING)
    repo.set_task_status(conn, task.id, TaskStatus.COMPLETED)
    with pytest.raises(InvalidTransition):
        repo.set_task_status(conn, task.id, TaskStatus.RUNNING)


def test_retry_increments_attempts_and_records_error(conn):
    agent = new_agent(conn)
    mission = repo.create_mission(conn, "x")
    task = repo.create_task(conn, mission.id, "t1", agent.id, "do it")
    repo.set_task_status(conn, task.id, TaskStatus.READY)
    repo.set_task_status(conn, task.id, TaskStatus.RUNNING)
    repo.set_task_status(conn, task.id, TaskStatus.READY, error="boom")  # retry
    task = repo.set_task_status(conn, task.id, TaskStatus.RUNNING)
    assert task.attempts == 2
    assert task.last_error == "boom"


def test_unknown_task_raises_lookup_error(conn):
    import uuid
    with pytest.raises(LookupError):
        repo.set_task_status(conn, uuid.uuid4(), TaskStatus.READY)