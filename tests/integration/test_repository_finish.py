from core import repository as repo
from core.models import MissionStatus


def test_finishing_a_running_mission_records_the_outcome(conn):
    mission = repo.create_mission(conn, "x")
    repo.set_mission_status(conn, mission.id, MissionStatus.RUNNING)

    done = repo.finish_mission_if_running(conn, mission.id, MissionStatus.COMPLETED, result="ok")

    assert done.status == MissionStatus.COMPLETED
    assert done.result == "ok"
    assert done.completed_at is not None


def test_a_mission_can_only_be_finished_once(conn):
    mission = repo.create_mission(conn, "x")
    repo.set_mission_status(conn, mission.id, MissionStatus.RUNNING)
    repo.finish_mission_if_running(conn, mission.id, MissionStatus.COMPLETED, result="first")

    second = repo.finish_mission_if_running(conn, mission.id, MissionStatus.FAILED, error="late")

    assert second is None
    stored = repo.get_mission(conn, mission.id)
    assert stored.status == MissionStatus.COMPLETED
    assert stored.result == "first" and stored.error is None


def test_a_mission_that_never_started_is_not_finished(conn):
    mission = repo.create_mission(conn, "x")  # still pending
    assert repo.finish_mission_if_running(conn, mission.id, MissionStatus.COMPLETED) is None
    assert repo.get_mission(conn, mission.id).status == MissionStatus.PENDING