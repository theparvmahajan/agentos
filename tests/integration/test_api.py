import uuid

import pytest
from fastapi.testclient import TestClient

from core import repository as repo
from core.db import get_conn
from core.models import MissionStatus, TaskStatus
from services.api.deps import get_database_url, get_launcher
from services.api.main import app

MARK = "[api-test] "
AGENT_NAME = "api-test-agent"


@pytest.fixture
def api(test_db_url):
    processed = []
    app.dependency_overrides[get_database_url] = lambda: test_db_url
    app.dependency_overrides[get_launcher] = lambda: processed.append
    yield TestClient(app), processed
    app.dependency_overrides.clear()
    with get_conn(test_db_url) as conn:
        conn.execute("DELETE FROM missions WHERE objective LIKE %s", (MARK + "%",))
        conn.execute("DELETE FROM agents WHERE name = %s", (AGENT_NAME,))


# ---------- POST /missions ----------

def test_post_creates_a_pending_mission_and_schedules_it(api, test_db_url):
    client, processed = api

    resp = client.post("/missions", json={"objective": MARK + "compare databases"})

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "pending"
    assert body["objective"] == MARK + "compare databases"
    assert body["tasks"] == []
    assert resp.headers["location"] == f"/missions/{body['id']}"
    assert processed == [uuid.UUID(body["id"])]  # the background job got exactly this mission

    with get_conn(test_db_url) as conn:  # and the row is really committed
        assert repo.get_mission(conn, uuid.UUID(body["id"])).status == MissionStatus.PENDING


def test_post_strips_whitespace(api):
    client, _ = api
    resp = client.post("/missions", json={"objective": f"   {MARK}padded   "})
    assert resp.json()["objective"] == MARK + "padded"


@pytest.mark.parametrize(
    "payload",
    [{}, {"objective": ""}, {"objective": "    "}, {"objective": "x" * 2001}, {"objective": 5}, []],
)
def test_post_rejects_invalid_input_without_creating_anything(api, payload):
    client, processed = api
    resp = client.post("/missions", json=payload)
    assert resp.status_code == 422
    assert processed == []


# ---------- GET /missions/{id} ----------

def test_get_unknown_mission_is_404(api):
    client, _ = api
    resp = client.get(f"/missions/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_get_malformed_id_is_422(api):
    client, _ = api
    assert client.get("/missions/not-a-uuid").status_code == 422


def test_get_a_fresh_mission_has_no_tasks_yet(api):
    client, _ = api
    created = client.post("/missions", json={"objective": MARK + "fresh"}).json()

    body = client.get(f"/missions/{created['id']}").json()

    assert body["status"] == "pending"
    assert body["tasks"] == []
    assert body["result"] is None and body["error"] is None


def test_get_returns_tasks_in_dependency_order_with_keys_and_details(api, test_db_url):
    client, _ = api
    with get_conn(test_db_url) as conn:
        agent = repo.create_agent(conn, AGENT_NAME)
        mission = repo.create_mission(conn, MARK + "detailed")
        # Created in one transaction, so the database returns them sorted by key:
        # "a_summary" comes back before "z_research" even though it depends on it.
        research = repo.create_task(conn, mission.id, "z_research", agent.id, "research it")
        summary = repo.create_task(
            conn, mission.id, "a_summary", agent.id, "summarize", depends_on=[research.id]
        )
        repo.set_task_status(conn, research.id, TaskStatus.READY)
        repo.set_task_status(conn, research.id, TaskStatus.RUNNING)
        repo.set_task_status(conn, research.id, TaskStatus.COMPLETED, output="found things")
        repo.set_task_status(conn, summary.id, TaskStatus.SKIPPED)
        repo.set_mission_status(conn, mission.id, MissionStatus.FAILED, error="something broke")

    body = client.get(f"/missions/{mission.id}").json()

    assert body["status"] == "failed"
    assert body["error"] == "something broke"
    assert [t["key"] for t in body["tasks"]] == ["z_research", "a_summary"]

    first, second = body["tasks"]
    assert first["agent"] == AGENT_NAME
    assert first["status"] == "completed"
    assert first["output"] == "found things"
    assert first["attempts"] == 1
    assert first["depends_on"] == []
    assert second["status"] == "skipped"
    assert second["depends_on"] == ["z_research"]  # keys, not internal ids


def test_responses_do_not_expose_internal_fields(api):
    client, _ = api
    created = client.post("/missions", json={"objective": MARK + "private"}).json()
    body = client.get(f"/missions/{created['id']}").json()
    assert "tenant_id" not in body


def test_health_still_works(api):
    client, _ = api
    assert client.get("/health").json() == {"status": "ok"}