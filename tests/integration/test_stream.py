"""WS /api/v1/missions/{id}/stream against the real app, listener and Postgres."""

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from core import repository as repo
from core.config import settings
from core.db import get_conn
from core.models import MissionStatus, TaskStatus
from services.api import stream
from services.api.main import app

MARK = "[stream-test] "
AGENT = "stream-test-agent"


@pytest.fixture
def client(test_db_url, monkeypatch):
    monkeypatch.setattr(settings, "database_url", test_db_url)  # app, listener and queries
    with TestClient(app) as c:
        assert app.state.listener.listening.wait(5), "the database listener did not start"
        yield c


@pytest.fixture
def agent_id(test_db_url):
    with get_conn(test_db_url) as conn:
        agent = repo.create_agent(conn, AGENT)
    yield agent.id
    with get_conn(test_db_url) as conn:
        conn.execute("DELETE FROM missions WHERE objective LIKE %s", (MARK + "%",))
        conn.execute("DELETE FROM agents WHERE id = %s", (agent.id,))


@pytest.fixture
def make_mission(test_db_url, agent_id):
    def _make(status=MissionStatus.RUNNING, keys=("a", "b")):
        with get_conn(test_db_url) as conn:
            mission = repo.create_mission(conn, MARK + uuid.uuid4().hex[:6])
            ids = {}
            for i, key in enumerate(keys):
                task = repo.create_task(
                    conn, mission.id, key, agent_id, f"do {key}",
                    depends_on=[ids[keys[i - 1]]] if i else [],
                )
                ids[key] = task.id
            repo.set_mission_status(conn, mission.id, status)
        return mission.id, ids

    return _make


def move(test_db_url, task_id, *statuses, output=None):
    with get_conn(test_db_url) as conn:
        for status in statuses[:-1]:
            repo.set_task_status(conn, task_id, status)
        repo.set_task_status(conn, task_id, statuses[-1], output=output)


def url(mission_id) -> str:
    return f"/api/v1/missions/{mission_id}/stream"


def test_the_first_message_is_a_snapshot_of_the_whole_mission(client, make_feed, make_mission):
    mission_id, _ = make_mission()
    with client.websocket_connect(url(mission_id)) as ws:
        first = make_feed(ws).next()

    assert first["type"] == "snapshot"
    mission = first["mission"]
    assert mission["id"] == str(mission_id) and mission["status"] == "running"
    assert [(t["key"], t["status"], t["depends_on"]) for t in mission["tasks"]] == [
        ("a", "pending", []),
        ("b", "pending", ["a"]),
    ]


def test_a_task_change_arrives_as_its_complete_current_state(
    client, test_db_url, make_feed, make_mission
):
    mission_id, ids = make_mission()
    with client.websocket_connect(url(mission_id)) as ws:
        feed = make_feed(ws)
        feed.next()  # snapshot
        move(test_db_url, ids["a"], TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.COMPLETED,
             output="the answer")

        seen = []
        while not (seen and seen[-1]["task"]["status"] == "completed"):
            seen.append(feed.next())

    last = seen[-1]
    assert last["type"] == "task" and last["mission_id"] == str(mission_id)
    assert last["task"]["key"] == "a" and last["task"]["output"] == "the answer"
    statuses = [m["task"]["status"] for m in seen]
    order = ["pending", "ready", "running", "completed"]
    assert statuses == sorted(statuses, key=order.index)  # only ever moves forward


def test_a_streamed_task_is_identical_to_the_same_task_from_the_rest_endpoint(
    client, test_db_url, make_feed, make_mission
):
    mission_id, ids = make_mission()
    with client.websocket_connect(url(mission_id)) as ws:
        feed = make_feed(ws)
        feed.next()
        move(test_db_url, ids["a"], TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.COMPLETED,
             output="same")
        message = feed.next()
        while message["task"]["status"] != "completed":
            message = feed.next()

    rest = client.get(f"/api/v1/missions/{mission_id}").json()
    rest_task = next(t for t in rest["tasks"] if t["key"] == "a")
    assert message["task"] == rest_task


def test_a_finished_mission_is_announced_and_the_server_then_closes_with_1000(
    client, test_db_url, make_feed, make_mission
):
    mission_id, _ = make_mission()
    with client.websocket_connect(url(mission_id)) as ws:
        feed = make_feed(ws)
        feed.next()
        with get_conn(test_db_url) as conn:
            repo.set_mission_status(conn, mission_id, MissionStatus.COMPLETED, result="done")
        messages, code = feed.until_closed()

    final = messages[-1]
    assert final["type"] == "mission"
    assert final["mission"]["status"] == "completed" and final["mission"]["result"] == "done"
    assert code == 1000


def test_a_mission_that_does_not_exist_closes_with_4404(client, make_feed):
    with client.websocket_connect(url(uuid.uuid4())) as ws:
        assert make_feed(ws).close_code() == 4404


def test_an_already_finished_mission_gets_its_snapshot_then_a_clean_close(
    client, make_feed, make_mission
):
    mission_id, _ = make_mission(status=MissionStatus.COMPLETED)
    with client.websocket_connect(url(mission_id)) as ws:
        messages, code = make_feed(ws).until_closed()
    assert [m["type"] for m in messages] == ["snapshot"] and code == 1000


def test_a_malformed_mission_id_is_refused_at_the_handshake(client):
    with pytest.raises(WebSocketDisconnect) as refused, client.websocket_connect(
        "/api/v1/missions/not-a-uuid/stream"
    ):
        pass
    assert refused.value.code == 1008  # policy violation: the request itself was invalid


def test_every_watcher_of_a_mission_hears_it_and_other_missions_stay_silent(
    client, test_db_url, make_feed, make_mission
):
    watched, ids = make_mission()
    other, _ = make_mission()
    with (
        client.websocket_connect(url(watched)) as one,
        client.websocket_connect(url(watched)) as two,
        client.websocket_connect(url(other)) as elsewhere,
    ):
        feeds = [make_feed(one), make_feed(two), make_feed(elsewhere)]
        for feed in feeds:
            assert feed.next()["type"] == "snapshot"

        move(test_db_url, ids["a"], TaskStatus.READY)

        assert feeds[0].next()["task"]["status"] == "ready"
        assert feeds[1].next()["task"]["status"] == "ready"
        feeds[2].assert_silent()


def test_changes_to_a_mission_nobody_watches_cost_no_database_reads(
    client, test_db_url, make_mission, monkeypatch
):
    reads = []
    real = stream.build_message
    monkeypatch.setattr(stream, "build_message", lambda *a, **k: reads.append(1) or real(*a, **k))
    _, ids = make_mission()
    move(test_db_url, ids["a"], TaskStatus.READY, TaskStatus.RUNNING)
    time.sleep(0.8)  # long enough for the listener to have seen every notification
    assert reads == []


def test_a_client_that_falls_far_behind_is_cut_off_with_1013(client, make_feed, make_mission):
    mission_id, _ = make_mission()
    hub = app.state.hub
    with client.websocket_connect(url(mission_id)) as ws:
        feed = make_feed(ws)
        feed.next()  # snapshot: the connection is now subscribed
        # Publish more than the queue holds in ONE step on the event loop, so the handler
        # cannot drain it in between: a deterministic overflow.
        client.portal.call(lambda: [hub.publish(mission_id, {"n": n}) for n in range(500)])
        assert feed.close_code(timeout=10) == 1013


def test_a_resync_sends_a_fresh_snapshot_of_the_current_state(
    client, test_db_url, make_feed, make_mission
):
    mission_id, ids = make_mission()
    with client.websocket_connect(url(mission_id)) as ws:
        feed = make_feed(ws)
        feed.next()
        with get_conn(test_db_url) as conn:  # change state without waiting for its message
            repo.set_task_status(conn, ids["a"], TaskStatus.READY)
        client.portal.call(app.state.hub.resync_all)

        snapshots = []
        while True:
            message = feed.next()
            if message["type"] == "snapshot":
                snapshots.append(message)
                break

    a = next(t for t in snapshots[0]["mission"]["tasks"] if t["key"] == "a")
    assert a["status"] == "ready"


def test_the_subscription_is_removed_when_the_client_leaves(client, make_feed, make_mission):
    mission_id, _ = make_mission()
    with client.websocket_connect(url(mission_id)) as ws:
        make_feed(ws).next()
        assert app.state.hub.is_watched(mission_id)
    deadline = time.time() + 3
    while app.state.hub.is_watched(mission_id) and time.time() < deadline:
        time.sleep(0.02)
    assert not app.state.hub.is_watched(mission_id)


def test_without_the_event_feed_the_socket_says_so_with_1011(test_db_url, monkeypatch, make_feed):
    monkeypatch.setattr(settings, "database_url", test_db_url)
    app.state.hub = None
    with TestClient(app) as outer:  # starts a feed; replace it with "none" afterwards
        app.state.hub = None
        with outer.websocket_connect(url(uuid.uuid4())) as ws:
            assert make_feed(ws).close_code() == 1011
