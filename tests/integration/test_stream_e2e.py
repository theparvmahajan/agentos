"""The whole system at once: API (planner) -> scheduler -> workers -> database trigger ->
listener -> WebSocket. The client rebuilds the mission ONLY from the messages it receives and
must end up identical to what the REST endpoint says."""

import json
import threading
import time
from functools import partial

import pytest
from fastapi.testclient import TestClient

from agents.sleeper import run_sleeper
from core import repository as repo
from core.config import settings
from core.db import get_conn
from core.llm.fake import FakeProvider
from core.pipeline import plan_and_publish
from core.scheduler import run_scheduler
from core.tools import ToolRegistry
from core.worker import run_worker
from services.api.deps import get_launcher
from services.api.main import app

AGENT = "it-e2e-sleeper"
MARK = "[stream-e2e] "
ORDER = ["pending", "ready", "running", "completed"]


@pytest.fixture
def system(test_db_url, queues, monkeypatch):
    """The API with its real listener, plus a scheduler and two workers on threads."""
    monkeypatch.setattr(settings, "database_url", test_db_url)
    with get_conn(test_db_url) as conn:
        repo.create_agent(conn, AGENT)

    def configure(plan: list[dict]):
        llm = FakeProvider([json.dumps({"tasks": plan})])
        app.dependency_overrides[get_launcher] = lambda: partial(
            plan_and_publish, queues=queues, provider=llm, database_url=test_db_url
        )

    running = []
    for target, name, extra in [
        (run_scheduler, "sched", {}),
        (run_worker, "w0", {"runners": {AGENT: run_sleeper}, "tools": ToolRegistry()}),
        (run_worker, "w1", {"runners": {AGENT: run_sleeper}, "tools": ToolRegistry()}),
    ]:
        stop = threading.Event()
        thread = threading.Thread(
            target=target, args=(queues,), daemon=True,
            kwargs={"consumer": name, "stop": stop, "database_url": test_db_url,
                    "block_ms": 50, "retry_delay": 0.05, **extra},
        )
        thread.start()
        running.append((stop, thread))

    with TestClient(app) as client:
        assert app.state.listener.listening.wait(5)
        yield client, configure

    app.dependency_overrides.clear()
    for stop, _ in running:
        stop.set()
    for _, thread in running:
        thread.join(5)
    with get_conn(test_db_url) as conn:
        conn.execute("DELETE FROM missions WHERE objective LIKE %s", (MARK + "%",))
        conn.execute("DELETE FROM agents WHERE name = %s", (AGENT,))


def task(key, deps, seconds):
    return {"key": key, "agent": AGENT, "instruction": f"sleep {seconds}", "depends_on": deps}


def fold(messages: list[dict]) -> dict:
    """What a client would hold after applying the messages in order."""
    mission, tasks = None, {}
    for m in messages:
        if m["type"] == "snapshot":
            mission = dict(m["mission"])
            tasks = {t["key"]: t for t in mission.pop("tasks")}
        elif m["type"] == "task":
            tasks[m["task"]["key"]] = m["task"]
        elif m["type"] == "mission":
            mission = {**mission, **m["mission"]}
    return {"mission": mission, "tasks": tasks}


def test_the_stream_alone_is_enough_to_rebuild_the_finished_mission(system, make_feed):
    client, configure = system
    configure([task("a", [], 0.15), task("b", [], 0.15), task("c", ["a", "b"], 0.15)])

    created = client.post("/api/v1/missions", json={"objective": MARK + "diamond-ish"})
    assert created.status_code == 202
    mission_id = created.json()["id"]
    with client.websocket_connect(f"/api/v1/missions/{mission_id}/stream") as ws:
        messages, code = make_feed(ws).until_closed(timeout=30)

    assert code == 1000 and messages[0]["type"] == "snapshot"
    assert messages[-1]["type"] == "mission" and messages[-1]["mission"]["status"] == "completed"

    # Per task, the reported status only ever moves forward.
    for key in ("a", "b", "c"):
        seen = [m["task"]["status"] for m in messages if m["type"] == "task"
                and m["task"]["key"] == key]
        assert seen == sorted(seen, key=ORDER.index) and seen[-1] == "completed"

    # And a client that applied only these messages holds exactly what the REST API reports.
    rebuilt = fold(messages)
    rest = client.get(f"/api/v1/missions/{mission_id}").json()
    assert rebuilt["mission"]["status"] == rest["status"] == "completed"
    assert rebuilt["mission"]["result"] == rest["result"]
    assert rebuilt["tasks"] == {t["key"]: t for t in rest["tasks"]}


def test_the_two_independent_tasks_are_seen_running_at_the_same_time(system, make_feed):
    client, configure = system
    configure([task("a", [], 0.4), task("b", [], 0.4)])
    mission_id = client.post("/api/v1/missions", json={"objective": MARK + "parallel"}).json()["id"]

    with client.websocket_connect(f"/api/v1/missions/{mission_id}/stream") as ws:
        messages, _ = make_feed(ws).until_closed(timeout=30)

    # Replay the messages: at some moment both tasks must be 'running' together.
    state, together = {}, False
    for m in messages:
        if m["type"] == "task":
            state[m["task"]["key"]] = m["task"]["status"]
        elif m["type"] == "snapshot":
            state = {t["key"]: t["status"] for t in m["mission"]["tasks"]}
        together = together or list(state.values()).count("running") == 2
    assert together


def test_cancelling_a_running_mission_through_the_api_stops_it_and_tells_the_stream(
    system, make_feed
):
    client, configure = system
    configure([task("a", [], 0.6), task("b", ["a"], 0.6), task("c", ["b"], 0.6)])
    mission_id = client.post("/api/v1/missions", json={"objective": MARK + "cancel me"}).json()["id"]

    with client.websocket_connect(f"/api/v1/missions/{mission_id}/stream") as ws:
        feed = make_feed(ws)
        seen = []
        while not any(m.get("task", {}).get("status") == "running" for m in seen):
            seen.append(feed.next(timeout=15))  # wait until the first task is really running
        assert client.post(f"/api/v1/missions/{mission_id}/cancel").status_code == 200
        rest, code = feed.until_closed(timeout=15)

    assert code == 1000
    assert rest[-1]["type"] == "mission" and rest[-1]["mission"]["status"] == "cancelled"

    time.sleep(1.5)  # long enough for the running task to finish and try to save its result
    final = client.get(f"/api/v1/missions/{mission_id}").json()
    by_key = {t["key"]: t for t in final["tasks"]}
    assert final["status"] == "cancelled"
    assert {t["status"] for t in by_key.values()} == {"cancelled"}
    assert by_key["a"]["output"] is None  # finished after the cancel: result discarded
    assert by_key["b"]["started_at"] is None and by_key["c"]["started_at"] is None  # never ran


def test_cancelling_the_mission_stops_every_watcher_at_once(system, make_feed):
    client, configure = system
    configure([task("a", [], 1.0), task("b", ["a"], 1.0)])
    mission_id = client.post("/api/v1/missions", json={"objective": MARK + "two watchers"}).json()["id"]
    url = f"/api/v1/missions/{mission_id}/stream"

    with client.websocket_connect(url) as one, client.websocket_connect(url) as two:
        feeds = [make_feed(one), make_feed(two)]
        for feed in feeds:
            assert feed.next(timeout=10)["type"] == "snapshot"
        client.post(f"/api/v1/missions/{mission_id}/cancel")
        results = [feed.until_closed(timeout=15) for feed in feeds]

    assert all(code == 1000 and msgs[-1]["mission"]["status"] == "cancelled" for msgs, code in results)
