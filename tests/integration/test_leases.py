"""Leases, fencing and the reaper, against a real Postgres and Redis."""

import threading
from datetime import timedelta

import pytest

from core import repository as repo
from core.db import get_conn
from core.models import MissionStatus, TaskStatus
from core.reaper import reap_expired
from core.scheduler import schedule_mission
from core.state import InvalidTransition

TTL = 30.0


def expire_lease(url, task_id):
    with get_conn(url) as conn:
        conn.execute(
            "UPDATE task_leases SET expires_at = now() - interval '1 second' WHERE task_id = %s",
            (task_id,),
        )


def lease_of(url, task_id):
    with get_conn(url) as conn:
        return conn.execute("SELECT * FROM task_leases WHERE task_id = %s", (task_id,)).fetchone()


def task_of(url, task_id):
    with get_conn(url) as conn:
        return repo.get_task(conn, task_id)


def claim(url, task_id, owner="w1", ttl=TTL):
    with get_conn(url) as conn:
        return repo.claim_task(conn, task_id, owner, ttl)


def reap(url, queues, **kwargs):
    return reap_expired(queues, lease_seconds=kwargs.pop("lease_seconds", TTL), database_url=url, **kwargs)


# ---------- claiming and renewing ----------

def test_claiming_runs_the_task_and_takes_a_lease(test_db_url, lease_mission):
    _, ids = lease_mission()
    task = claim(test_db_url, ids["a"], owner="worker-7")

    assert task.status == TaskStatus.RUNNING and task.attempts == 1
    lease = lease_of(test_db_url, ids["a"])
    assert lease["owner"] == "worker-7" and lease["attempt"] == 1
    with get_conn(test_db_url) as conn:
        remaining = conn.execute(
            "SELECT extract(epoch FROM expires_at - now()) AS s FROM task_leases WHERE task_id = %s",
            (ids["a"],),
        ).fetchone()["s"]
    assert TTL - 5 < remaining <= TTL  # measured on the database's clock


def test_a_failed_claim_changes_nothing_and_leaves_no_lease(test_db_url, lease_mission):
    _, ids = lease_mission()
    claim(test_db_url, ids["a"], owner="first")
    with pytest.raises(InvalidTransition):
        claim(test_db_url, ids["a"], owner="second")
    assert lease_of(test_db_url, ids["a"])["owner"] == "first"
    assert task_of(test_db_url, ids["a"]).attempts == 1


def test_renewing_pushes_the_expiry_forward_only_for_the_holder_of_the_current_attempt(
    test_db_url, lease_mission
):
    _, ids = lease_mission()
    claim(test_db_url, ids["a"], owner="w1", ttl=5)
    before = lease_of(test_db_url, ids["a"])["expires_at"]

    with get_conn(test_db_url) as conn:
        assert repo.renew_lease(conn, ids["a"], "w1", 1, 60) is True
        assert repo.renew_lease(conn, ids["a"], "someone-else", 1, 60) is False
        assert repo.renew_lease(conn, ids["a"], "w1", 2, 60) is False  # wrong attempt
    assert lease_of(test_db_url, ids["a"])["expires_at"] > before + timedelta(seconds=30)


def test_a_finished_task_cannot_be_renewed(test_db_url, lease_mission):
    _, ids = lease_mission()
    claim(test_db_url, ids["a"])
    with get_conn(test_db_url) as conn:
        repo.set_task_status(conn, ids["a"], TaskStatus.COMPLETED, output="x", attempt=1)
        assert repo.renew_lease(conn, ids["a"], "w1", 1, 60) is False


# ---------- fencing ----------

def test_a_stale_attempt_cannot_overwrite_the_retry(test_db_url, lease_mission, queues):
    _, ids = lease_mission()
    claim(test_db_url, ids["a"], owner="zombie")  # attempt 1
    expire_lease(test_db_url, ids["a"])
    assert reap(test_db_url, queues).requeued == (ids["a"],)
    second = claim(test_db_url, ids["a"], owner="healthy")  # attempt 2
    assert second.attempts == 2
    lease = lease_of(test_db_url, ids["a"])
    assert (lease["owner"], lease["attempt"]) == ("healthy", 2)  # the new claim replaced the old lease

    with get_conn(test_db_url) as conn, pytest.raises(InvalidTransition, match="fenced to attempt 1"):
        repo.set_task_status(conn, ids["a"], TaskStatus.COMPLETED, output="stale", attempt=1)

    with get_conn(test_db_url) as conn:
        repo.set_task_status(conn, ids["a"], TaskStatus.COMPLETED, output="fresh", attempt=2)
    done = task_of(test_db_url, ids["a"])
    assert done.output == "fresh" and done.status == TaskStatus.COMPLETED


def test_without_a_token_the_old_behaviour_is_unchanged(test_db_url, lease_mission):
    _, ids = lease_mission()
    claim(test_db_url, ids["a"])
    with get_conn(test_db_url) as conn:
        repo.set_task_status(conn, ids["a"], TaskStatus.COMPLETED, output="ok")  # no attempt=
    assert task_of(test_db_url, ids["a"]).status == TaskStatus.COMPLETED


# ---------- the reaper ----------

def test_only_expired_leases_are_candidates_for_recovery(test_db_url, lease_mission):
    _, ids = lease_mission(keys=("live", "dead"))
    claim(test_db_url, ids["live"])
    claim(test_db_url, ids["dead"])
    expire_lease(test_db_url, ids["dead"])
    with get_conn(test_db_url) as conn:
        found = repo.find_orphaned_tasks(conn, lease_seconds=TTL)
    assert found == [(ids["dead"], 1)]


def test_an_expired_lease_with_attempts_left_is_requeued_and_republished(
    test_db_url, lease_mission, queues
):
    mission_id, ids = lease_mission()
    claim(test_db_url, ids["a"])
    expire_lease(test_db_url, ids["a"])

    report = reap(test_db_url, queues)

    assert report.requeued == (ids["a"],) and report.failed == ()
    task = task_of(test_db_url, ids["a"])
    assert task.status == TaskStatus.READY and task.attempts == 1
    assert "lease expired" in task.last_error
    [message] = queues.tasks.read("w")
    assert message.fields == {"mission_id": str(mission_id), "task_id": str(ids["a"])}


def test_with_no_attempts_left_the_task_fails_and_the_scheduler_is_told(
    test_db_url, lease_mission, queues
):
    mission_id, ids = lease_mission(max_attempts=1)
    claim(test_db_url, ids["a"])
    expire_lease(test_db_url, ids["a"])

    report = reap(test_db_url, queues)

    assert report.failed == (ids["a"],) and report.requeued == ()
    task = task_of(test_db_url, ids["a"])
    assert task.status == TaskStatus.FAILED and task.finished_at is not None
    assert "no attempts" not in task.last_error and "lease expired" in task.last_error
    assert queues.tasks.length() == 0  # not queued again
    [event] = queues.events.read("s")
    assert event.fields == {"mission_id": str(mission_id), "task_id": str(ids["a"])}

    schedule_mission(mission_id, queues, database_url=test_db_url)  # the scheduler reacts
    with get_conn(test_db_url) as conn:
        assert repo.get_mission(conn, mission_id).status == MissionStatus.FAILED


def test_the_reaper_leaves_healthy_and_finished_work_alone(test_db_url, lease_mission, queues):
    _, ids = lease_mission(keys=("live", "done", "queued", "cancelled"))
    claim(test_db_url, ids["live"])  # live lease, 30 s left
    claim(test_db_url, ids["done"])
    with get_conn(test_db_url) as conn:
        repo.set_task_status(conn, ids["done"], TaskStatus.COMPLETED, output="x", attempt=1)
        repo.set_task_status(conn, ids["cancelled"], TaskStatus.CANCELLED)
    expire_lease(test_db_url, ids["done"])  # an old lease on a finished task means nothing

    report = reap(test_db_url, queues)

    assert report.requeued == () and report.failed == ()
    assert task_of(test_db_url, ids["live"]).status == TaskStatus.RUNNING
    assert task_of(test_db_url, ids["done"]).status == TaskStatus.COMPLETED
    assert task_of(test_db_url, ids["queued"]).status == TaskStatus.READY
    assert queues.tasks.length() == 0 and queues.events.length() == 0


def test_a_running_task_with_no_lease_is_reaped_only_after_a_full_lease_period(
    test_db_url, lease_mission, queues
):
    """A task claimed by code from before leases existed has no lease row. It is given one full
    lease period of grace, so a rolling upgrade does not reap work that is still alive."""
    _, ids = lease_mission(keys=("old", "new"))
    with get_conn(test_db_url) as conn:  # claimed the old way: no lease
        repo.set_task_status(conn, ids["old"], TaskStatus.RUNNING)
        repo.set_task_status(conn, ids["new"], TaskStatus.RUNNING)
        conn.execute(
            "UPDATE tasks SET updated_at = now() - interval '2 minutes' WHERE id = %s", (ids["old"],)
        )

    report = reap(test_db_url, queues, lease_seconds=60)

    assert report.requeued == (ids["old"],)
    assert task_of(test_db_url, ids["new"]).status == TaskStatus.RUNNING


def test_only_the_current_attempts_lease_counts(test_db_url, lease_mission, queues):
    _, ids = lease_mission()
    claim(test_db_url, ids["a"])  # attempt 1
    with get_conn(test_db_url) as conn:  # a leftover lease row from an earlier attempt
        conn.execute("UPDATE task_leases SET attempt = 0 WHERE task_id = %s", (ids["a"],))
    assert reap(test_db_url, queues).requeued == ()  # no lease for attempt 1, but still in its grace period


@pytest.mark.parametrize("round_", range(15))
def test_two_reapers_racing_recover_a_task_exactly_once(test_db_url, lease_mission, queues, round_):
    _, ids = lease_mission()
    claim(test_db_url, ids["a"])
    expire_lease(test_db_url, ids["a"])
    barrier = threading.Barrier(2)
    reports = []

    def go():
        barrier.wait()
        reports.append(reap(test_db_url, queues))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert sum(len(r.requeued) for r in reports) == 1
    assert queues.tasks.length() == 1  # one message, not two
    assert task_of(test_db_url, ids["a"]).status == TaskStatus.READY
