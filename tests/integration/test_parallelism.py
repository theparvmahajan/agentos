"""Does the real scheduler + workers pipeline actually run independent tasks in parallel, and
never break dependencies? Real Postgres and Redis, workers as threads, sleeping tasks.

Timing assertions use only generous bounds: a LOWER bound that is physically forced (one worker
cannot finish N sleeps faster than N x duration) and an UPPER bound with 3x slack over the ideal.
"""

import threading
import time

import pytest

from agents.sleeper import run_sleeper
from core import repository as repo
from core.db import get_conn
from core.models import MissionStatus
from core.scheduler import run_scheduler
from core.tools import ToolRegistry
from core.worker import run_worker
from scripts.benchmark import chain, create_mission, diamond

AGENT = "it-sleeper"
D = 0.4  # seconds each task sleeps


@pytest.fixture
def agent_id(test_db_url):
    with get_conn(test_db_url) as conn:
        agent = repo.create_agent(conn, AGENT)
    yield agent.id
    with get_conn(test_db_url) as conn:
        conn.execute("DELETE FROM missions WHERE objective LIKE '[bench] it-%'")
        conn.execute("DELETE FROM agents WHERE id = %s", (agent.id,))


@pytest.fixture
def cluster(test_db_url, queues):
    running = []

    def start(workers: int) -> None:
        def launch(target, name, **extra):
            stop = threading.Event()
            thread = threading.Thread(
                target=target, args=(queues,), daemon=True,
                kwargs={"consumer": name, "stop": stop, "database_url": test_db_url,
                        "block_ms": 50, "retry_delay": 0.05, **extra},
            )
            thread.start()
            running.append((stop, thread))

        launch(run_scheduler, "sched")
        for i in range(workers):
            launch(run_worker, f"w{i}", runners={AGENT: run_sleeper}, tools=ToolRegistry())

    yield start
    for stop, _ in running:
        stop.set()
    for _, thread in running:
        thread.join(timeout=5)
        assert not thread.is_alive()


def run(test_db_url, queues, agent_id, shape, label):
    """Returns (wall seconds, {task key: (started_at, finished_at)})."""
    mission_id = create_mission(shape, D, agent_id, f"it-{label}", test_db_url)
    start = time.perf_counter()
    queues.publish_event(mission_id)
    deadline = time.time() + 30
    while time.time() < deadline:
        with get_conn(test_db_url) as conn:
            mission = repo.get_mission(conn, mission_id)
        if mission.status in (MissionStatus.COMPLETED, MissionStatus.FAILED):
            break
        time.sleep(0.01)
    wall = time.perf_counter() - start
    assert mission.status == MissionStatus.COMPLETED, mission.error
    with get_conn(test_db_url) as conn:
        spans = {t.key: (t.started_at, t.finished_at) for t in repo.get_tasks(conn, mission_id)}
    return wall, spans


def max_concurrency(spans) -> int:
    events = [(s, 1) for s, _ in spans.values()] + [(f, -1) for _, f in spans.values()]
    running = best = 0
    for _, delta in sorted(events, key=lambda e: (e[0], e[1])):  # ends sort before starts on ties
        running += delta
        best = max(best, running)
    return best


INDEPENDENT_4 = {"a": [], "b": [], "c": [], "d": []}


def test_four_workers_run_four_independent_tasks_at_the_same_time(
    test_db_url, queues, agent_id, cluster
):
    cluster(4)
    wall, spans = run(test_db_url, queues, agent_id, INDEPENDENT_4, "par4")

    assert wall < 3 * D  # ideal is D; sequential would need 4 x D
    assert max_concurrency(spans) >= 3


def test_one_worker_cannot_beat_the_sum_of_the_sleeps_and_never_overlaps(
    test_db_url, queues, agent_id, cluster
):
    cluster(1)
    wall, spans = run(test_db_url, queues, agent_id, INDEPENDENT_4, "par1")

    assert wall >= 4 * D  # physically forced
    assert max_concurrency(spans) == 1


def test_a_chain_never_overlaps_even_with_many_workers(test_db_url, queues, agent_id, cluster):
    cluster(4)
    wall, spans = run(test_db_url, queues, agent_id, chain(3), "chain")

    assert wall >= 3 * D
    assert max_concurrency(spans) == 1
    for earlier, later in (("t1", "t2"), ("t2", "t3")):
        assert spans[later][0] >= spans[earlier][1]  # started only after its dependency finished


def test_a_diamond_runs_its_middle_in_parallel_and_its_end_after_both(
    test_db_url, queues, agent_id, cluster
):
    cluster(4)
    wall, spans = run(test_db_url, queues, agent_id, diamond(), "diamond")

    assert spans["b"][0] < spans["c"][1] and spans["c"][0] < spans["b"][1]  # b and c overlap
    assert spans["d"][0] >= max(spans["b"][1], spans["c"][1])
    assert 3 * D <= wall < 4 * D + 0.5  # three rounds, not four
