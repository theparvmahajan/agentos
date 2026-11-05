"""GET /api/v1/missions (paged list) and POST /api/v1/missions/{id}/cancel."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from core import repository as repo
from core.db import get_conn
from core.models import MissionStatus, TaskStatus
from services.api.deps import get_database_url
from services.api.main import app

AGENT = "list-test-agent"
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def api(test_db_url):
    with get_conn(test_db_url) as conn:
        conn.execute("DELETE FROM missions")  # the test database is ours: start from empty
        agent = repo.create_agent(conn, AGENT)
    app.dependency_overrides[get_database_url] = lambda: test_db_url
    yield TestClient(app), test_db_url, agent.id
    app.dependency_overrides.clear()
    with get_conn(test_db_url) as conn:
        conn.execute("DELETE FROM missions")
        conn.execute("DELETE FROM agents WHERE id = %s", (agent.id,))


def add_mission(url, *, age_minutes=0, status=MissionStatus.PENDING, objective=None, created=None):
    with get_conn(url) as conn:
        mission = repo.create_mission(conn, objective or f"m-{uuid.uuid4().hex[:6]}")
        repo.set_mission_status(conn, mission.id, status)
        stamp = created or T0 - timedelta(minutes=age_minutes)
        conn.execute("UPDATE missions SET created_at = %s WHERE id = %s", (stamp, mission.id))
    return str(mission.id)


def page(client, **params):
    response = client.get("/api/v1/missions", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def walk(client, limit):
    """Follow next_cursor to the end. Returns every page's ids."""
    pages, cursor = [], None
    while True:
        body = page(client, limit=limit, **({"cursor": cursor} if cursor else {}))
        pages.append([m["id"] for m in body["items"]])
        cursor = body["next_cursor"]
        if cursor is None:
            return pages


# ---------- listing ----------

def test_an_empty_database_gives_an_empty_page(api):
    client, _, _ = api
    assert page(client) == {"items": [], "next_cursor": None}


def test_missions_come_back_newest_first(api):
    client, url, _ = api
    oldest, middle, newest = add_mission(url, age_minutes=30), add_mission(url, age_minutes=20), add_mission(url, age_minutes=10)
    assert [m["id"] for m in page(client)["items"]] == [newest, middle, oldest]


def test_paging_returns_every_mission_exactly_once(api):
    client, url, _ = api
    ids = [add_mission(url, age_minutes=n) for n in range(5)]  # ids[0] is the newest
    pages = walk(client, limit=2)
    assert [len(p) for p in pages] == [2, 2, 1]
    assert [i for p in pages for i in p] == ids


def test_missions_created_at_the_same_instant_are_not_lost_between_pages(api):
    client, url, _ = api
    ids = [add_mission(url, created=T0) for _ in range(7)]  # identical timestamps
    seen = [i for p in walk(client, limit=2) for i in p]
    assert sorted(seen) == sorted(ids) and len(seen) == len(set(seen))


def test_a_new_mission_between_two_requests_neither_repeats_nor_skips_older_ones(api):
    client, url, _ = api
    original = [add_mission(url, age_minutes=10 + n) for n in range(5)]
    first = page(client, limit=2)
    newcomer = add_mission(url, age_minutes=0)  # arrives while the client is paging
    rest, cursor = [], first["next_cursor"]
    while cursor:
        body = page(client, limit=2, cursor=cursor)
        rest += [m["id"] for m in body["items"]]
        cursor = body["next_cursor"]

    collected = [m["id"] for m in first["items"]] + rest
    assert collected == original and newcomer not in collected  # an offset would have repeated one


def test_a_full_last_page_has_no_next_cursor(api):
    client, url, _ = api
    for n in range(4):
        add_mission(url, age_minutes=n)
    assert page(client, limit=4)["next_cursor"] is None
    assert page(client, limit=3)["next_cursor"] is not None


def test_the_status_filter_works_alone_and_with_paging(api):
    client, url, _ = api
    done = [add_mission(url, age_minutes=n, status=MissionStatus.COMPLETED) for n in range(3)]
    add_mission(url, age_minutes=9, status=MissionStatus.RUNNING)
    add_mission(url, age_minutes=10, status=MissionStatus.FAILED)

    assert [m["id"] for m in page(client, status="completed")["items"]] == done
    first = page(client, status="completed", limit=2)
    second = page(client, status="completed", limit=2, cursor=first["next_cursor"])
    assert [m["id"] for m in first["items"] + second["items"]] == done
    assert second["next_cursor"] is None


def test_each_row_carries_task_counts(api):
    client, url, agent_id = api
    mission_id = add_mission(url, status=MissionStatus.RUNNING)
    with get_conn(url) as conn:
        for i, status in enumerate([TaskStatus.COMPLETED, TaskStatus.COMPLETED, TaskStatus.PENDING]):
            task = repo.create_task(conn, uuid.UUID(mission_id), f"t{i}", agent_id, "x")
            conn.execute("UPDATE tasks SET status = %s WHERE id = %s", (status.value, task.id))
    add_mission(url, age_minutes=5)  # a mission with no tasks at all

    rows = {m["id"]: m for m in page(client)["items"]}
    assert (rows[mission_id]["tasks_total"], rows[mission_id]["tasks_completed"]) == (3, 2)
    assert all(r["tasks_total"] == 0 for i, r in rows.items() if i != mission_id)


def test_rows_do_not_expose_internal_fields(api):
    client, url, _ = api
    add_mission(url)
    row = page(client)["items"][0]
    assert "tenant_id" not in row and "tasks" not in row and "result" not in row


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 101}, {"limit": "many"}, {"cursor": "garbage"}, {"status": "nope"}],
)
def test_invalid_query_parameters_are_refused_with_422(api, params):
    client, _, _ = api
    assert client.get("/api/v1/missions", params=params).status_code == 422


def test_the_old_unversioned_path_is_gone(api):
    client, _, _ = api
    assert client.get("/missions").status_code == 404


# ---------- cancelling ----------

def test_cancelling_a_pending_mission(api):
    client, url, _ = api
    mission_id = add_mission(url)
    response = client.post(f"/api/v1/missions/{mission_id}/cancel")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled" and body["completed_at"] is not None
    assert client.get(f"/api/v1/missions/{mission_id}").json()["status"] == "cancelled"


def test_cancelling_a_running_mission_cancels_every_unfinished_task_but_keeps_finished_ones(api):
    client, url, agent_id = api
    mission_id = add_mission(url, status=MissionStatus.RUNNING)
    states = {"done": "completed", "boom": "failed", "wait": "pending", "next": "ready",
              "busy": "running"}
    with get_conn(url) as conn:
        for key, status in states.items():
            task = repo.create_task(conn, uuid.UUID(mission_id), key, agent_id, "x")
            conn.execute("UPDATE tasks SET status = %s, output = %s WHERE id = %s",
                         (status, "kept" if status == "completed" else None, task.id))

    body = client.post(f"/api/v1/missions/{mission_id}/cancel").json()
    by_key = {t["key"]: t for t in body["tasks"]}

    assert {k: t["status"] for k, t in by_key.items()} == {
        "done": "completed", "boom": "failed",  # finished work is never rewritten
        "wait": "cancelled", "next": "cancelled", "busy": "cancelled",
    }
    assert by_key["done"]["output"] == "kept"


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_a_finished_mission_cannot_be_cancelled(api, status):
    client, url, _ = api
    mission_id = add_mission(url, status=MissionStatus(status))
    response = client.post(f"/api/v1/missions/{mission_id}/cancel")
    assert response.status_code == 409 and status in response.json()["detail"]
    assert client.get(f"/api/v1/missions/{mission_id}").json()["status"] == status


def test_cancelling_twice_is_a_conflict_not_an_error(api):
    client, url, _ = api
    mission_id = add_mission(url)
    assert client.post(f"/api/v1/missions/{mission_id}/cancel").status_code == 200
    assert client.post(f"/api/v1/missions/{mission_id}/cancel").status_code == 409


def test_cancelling_an_unknown_or_malformed_mission(api):
    client, _, _ = api
    assert client.post(f"/api/v1/missions/{uuid.uuid4()}/cancel").status_code == 404
    assert client.post("/api/v1/missions/not-a-uuid/cancel").status_code == 422


# ---------- the guards underneath (repository) ----------

def test_transition_mission_only_moves_from_the_allowed_states(conn):
    mission = repo.create_mission(conn, "x")
    assert repo.transition_mission(
        conn, mission.id, MissionStatus.RUNNING, allowed_from=[MissionStatus.PLANNING]
    ) is None  # it is pending, not planning: nothing changes
    assert repo.get_mission(conn, mission.id).status == MissionStatus.PENDING

    moved = repo.transition_mission(
        conn, mission.id, MissionStatus.PLANNING, allowed_from=[MissionStatus.PENDING]
    )
    assert moved.status == MissionStatus.PLANNING


def test_transition_mission_stamps_completion_only_for_terminal_states(conn):
    mission = repo.create_mission(conn, "x")
    running = repo.transition_mission(
        conn, mission.id, MissionStatus.RUNNING, allowed_from=[MissionStatus.PENDING]
    )
    assert running.completed_at is None
    failed = repo.transition_mission(
        conn, mission.id, MissionStatus.FAILED, allowed_from=[MissionStatus.RUNNING], error="bad"
    )
    assert failed.completed_at is not None and failed.error == "bad"


def test_cancel_mission_never_touches_a_finished_mission(conn):
    mission = repo.create_mission(conn, "x")
    repo.set_mission_status(conn, mission.id, MissionStatus.COMPLETED, result="r")
    assert repo.cancel_mission(conn, mission.id) is None
    assert repo.get_mission(conn, mission.id).status == MissionStatus.COMPLETED
    assert repo.cancel_mission(conn, uuid.uuid4()) is None
