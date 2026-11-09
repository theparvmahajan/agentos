"""POST /missions with an Idempotency-Key: repeating a request must not repeat its effect."""

import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from core import repository as repo
from core.db import get_conn
from services.api.deps import get_database_url, get_launcher
from services.api.main import app

MARK = "[idem-test] "


@pytest.fixture
def api(test_db_url):
    launched = []
    app.dependency_overrides[get_database_url] = lambda: test_db_url
    app.dependency_overrides[get_launcher] = lambda: launched.append
    yield TestClient(app), launched
    app.dependency_overrides.clear()
    with get_conn(test_db_url) as conn:  # the keys go with their missions (ON DELETE CASCADE)
        conn.execute("DELETE FROM missions WHERE objective LIKE %s", (MARK + "%",))


def post(client, objective, key=None):
    headers = {"Idempotency-Key": key} if key is not None else {}
    return client.post("/api/v1/missions", json={"objective": MARK + objective}, headers=headers)


def count_missions(url, objective):
    with get_conn(url) as conn:
        return conn.execute(
            "SELECT count(*) AS n FROM missions WHERE objective = %s", (MARK + objective,)
        ).fetchone()["n"]


# ---------- the API ----------

def test_repeating_a_request_returns_the_same_mission_and_starts_nothing_new(api, test_db_url):
    client, launched = api
    key = f"key-{uuid.uuid4()}"

    first = post(client, "compare databases", key)
    second = post(client, "compare databases", key)

    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert second.headers["location"] == first.headers["location"]
    assert "idempotent-replay" not in first.headers
    assert second.headers["idempotent-replay"] == "true"
    assert len(launched) == 1  # the planner was started once, not twice
    assert count_missions(test_db_url, "compare databases") == 1


def test_a_replay_shows_the_mission_as_it_is_now(api, test_db_url):
    client, _ = api
    key = f"key-{uuid.uuid4()}"
    created = post(client, "progressing", key).json()

    with get_conn(test_db_url) as conn:  # the mission moves on after the first response
        agent = repo.create_agent(conn, "idem-agent", capabilities=[])
        repo.create_task(conn, uuid.UUID(created["id"]), "a", agent.id, "do a")
    try:
        replay = post(client, "progressing", key).json()
        assert [t["key"] for t in replay["tasks"]] == ["a"]
    finally:
        with get_conn(test_db_url) as conn:
            conn.execute("DELETE FROM missions WHERE id = %s", (created["id"],))
            conn.execute("DELETE FROM agents WHERE id = %s", (agent.id,))


def test_different_keys_make_different_missions(api, test_db_url):
    client, launched = api
    a = post(client, "same words", f"key-{uuid.uuid4()}").json()["id"]
    b = post(client, "same words", f"key-{uuid.uuid4()}").json()["id"]
    assert a != b and len(launched) == 2


def test_without_a_key_every_request_is_new(api):
    client, launched = api
    a = post(client, "no key").json()["id"]
    b = post(client, "no key").json()["id"]
    assert a != b and len(launched) == 2


def test_the_same_key_with_a_different_objective_is_refused(api, test_db_url):
    client, launched = api
    key = f"key-{uuid.uuid4()}"
    post(client, "original", key)

    clash = post(client, "something else entirely", key)

    assert clash.status_code == 409 and "different objective" in clash.json()["detail"]
    assert len(launched) == 1
    assert count_missions(test_db_url, "something else entirely") == 0  # and nothing was created


@pytest.mark.parametrize("key", ["", "has space", "x" * 256, "tab\there"])
def test_a_malformed_key_is_rejected_before_anything_happens(api, test_db_url, key):
    client, launched = api
    response = post(client, "never created", key)
    assert response.status_code == 422
    assert launched == [] and count_missions(test_db_url, "never created") == 0


def test_a_long_but_legal_key_is_accepted(api):
    client, _ = api
    assert post(client, "long key", "k" * 255).status_code == 202


def test_browsers_are_allowed_to_send_the_header_and_read_the_replay_marker(api):
    client, _ = api
    preflight = client.options(
        "/api/v1/missions",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,idempotency-key",
        },
    )
    assert preflight.status_code == 200
    assert "idempotency-key" in preflight.headers["access-control-allow-headers"].lower()

    real = client.post(  # the exposed headers are on the real response, not on the preflight
        "/api/v1/missions",
        json={"objective": MARK + "cors"},
        headers={"Origin": "http://localhost:3000", "Idempotency-Key": f"cors-{uuid.uuid4()}"},
    )
    exposed = real.headers["access-control-expose-headers"].lower()
    assert "idempotent-replay" in exposed and "location" in exposed


# ---------- the repository: races and scopes ----------

@pytest.fixture
def cleanup(test_db_url):
    ids = []
    yield ids
    with get_conn(test_db_url) as conn:
        for mission_id in ids:
            conn.execute("DELETE FROM missions WHERE id = %s", (mission_id,))


@pytest.mark.parametrize("round_", range(10))
def test_eight_identical_requests_at_once_create_exactly_one_mission(
    test_db_url, cleanup, round_
):
    key, barrier, results = f"race-{uuid.uuid4()}", threading.Barrier(8), []

    def go():
        barrier.wait()
        with get_conn(test_db_url) as conn:
            results.append(repo.create_mission_idempotent(conn, MARK + "race", key, "hash"))

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    cleanup.extend({m.id for m, _ in results})

    assert len(results) == 8
    assert sum(1 for _, created in results if created) == 1  # one winner...
    assert len({m.id for m, _ in results}) == 1  # ...and everyone got its mission
    assert count_missions(test_db_url, "race") == 1  # the losers' missions were rolled back


def test_the_same_key_in_two_tenants_is_two_missions(test_db_url, cleanup):
    key = f"tenant-{uuid.uuid4()}"
    with get_conn(test_db_url) as conn:
        a, a_new = repo.create_mission_idempotent(conn, MARK + "t", key, "h", tenant_id=uuid.uuid4())
        b, b_new = repo.create_mission_idempotent(conn, MARK + "t", key, "h", tenant_id=uuid.uuid4())
        c, c_new = repo.create_mission_idempotent(conn, MARK + "t", key, "h")  # no tenant
    cleanup.extend([a.id, b.id, c.id])
    assert a.id != b.id != c.id and a_new and b_new and c_new


def test_a_conflicting_key_leaves_no_trace(test_db_url, cleanup):
    key = f"conflict-{uuid.uuid4()}"
    with get_conn(test_db_url) as conn:
        first, _ = repo.create_mission_idempotent(conn, MARK + "first", key, "hash-1")
    cleanup.append(first.id)
    with get_conn(test_db_url) as conn, pytest.raises(repo.IdempotencyConflict):
        repo.create_mission_idempotent(conn, MARK + "second", key, "hash-2")
    assert count_missions(test_db_url, "second") == 0
