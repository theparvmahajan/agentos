import uuid

from core import repository as repo


def test_get_task_returns_the_task_with_its_dependencies(conn):
    agent = repo.create_agent(conn, "researcher")
    mission = repo.create_mission(conn, "x")
    a = repo.create_task(conn, mission.id, "a", agent.id, "first")
    b = repo.create_task(conn, mission.id, "b", agent.id, "second", depends_on=[a.id])

    fetched = repo.get_task(conn, b.id)

    assert fetched.key == "b"
    assert fetched.depends_on == [a.id]
    assert repo.get_task(conn, a.id).depends_on == []


def test_get_task_for_an_unknown_id_is_none(conn):
    assert repo.get_task(conn, uuid.uuid4()) is None