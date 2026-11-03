"""The API end to end: POST /missions plans in the background and wakes the scheduler;
the scheduler and workers then run the tasks and GET shows the result. No in-process executor."""

import json
import threading
import time
from functools import partial

import pytest
from fastapi.testclient import TestClient

from agents.sleeper import run_sleeper
from core import repository as repo
from core.db import get_conn
from core.llm.fake import FakeProvider
from core.pipeline import plan_and_publish
from core.scheduler import run_scheduler
from core.tools import ToolRegistry
from core.worker import run_worker
from services.api.deps import get_database_url, get_launcher
from services.api.main import app

AGENT = "it-api-sleeper"
MARK = "[api-pipeline-test] "


@pytest.fixture
def stack(test_db_url, queues):
    with get_conn(test_db_url) as conn:
        repo.create_agent(conn, AGENT, description="sleeps", capabilities=[])
    llm = FakeProvider([json.dumps({"tasks": [
        {"key": "t1", "agent": AGENT, "instruction": "sleep 0.1", "depends_on": []},
        {"key": "t2", "agent": AGENT, "instruction": "sleep 0.1", "depends_on": []},
        {"key": "t3", "agent": AGENT, "instruction": "sleep 0.1", "depends_on": ["t1", "t2"]},
    ]})])

    app.dependency_overrides[get_database_url] = lambda: test_db_url
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

    yield TestClient(app), llm

    app.dependency_overrides.clear()
    for stop, _ in running:
        stop.set()
    for _, thread in running:
        thread.join(timeout=5)
    with get_conn(test_db_url) as conn:
        conn.execute("DELETE FROM missions WHERE objective LIKE %s", (MARK + "%",))
        conn.execute("DELETE FROM agents WHERE name = %s", (AGENT,))


def test_a_posted_mission_is_planned_scheduled_and_completed_by_the_workers(stack):
    client, llm = stack

    created = client.post("/missions", json={"objective": MARK + "two then one"})
    assert created.status_code == 202

    deadline = time.time() + 15
    while time.time() < deadline:
        body = client.get(created.headers["location"]).json()
        if body["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)

    assert body["status"] == "completed", body["error"]
    assert [t["key"] for t in body["tasks"]] == ["t1", "t2", "t3"]
    assert {t["status"] for t in body["tasks"]} == {"completed"}
    assert body["result"] == "slept 0.1s after 2 dependencies"  # t3 got both outputs
    assert len(llm.calls) == 1  # the API made exactly one LLM call: the plan
