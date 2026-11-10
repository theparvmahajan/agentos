import time
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
import redis

from core import repository as repo
from core.config import settings
from core.db import get_conn
from core.models import MissionStatus
from core.queue import build_queues
from core.runner import AgentResult
from tests.chaos.proxy import ChaosProxy
from tests.integration.conftest import LEASE_AGENT

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture
def redis_proxy():
    parts = urlsplit(settings.redis_url)
    proxy = ChaosProxy(parts.hostname or "localhost", parts.port or 6379)
    yield proxy
    proxy.close()


@pytest.fixture
def db_proxy(test_db_url):
    parts = urlsplit(test_db_url)
    proxy = ChaosProxy(parts.hostname or "localhost", parts.port or 5432)
    yield proxy
    proxy.close()


@pytest.fixture
def chain(test_db_url):
    """make(n) creates a RUNNING mission whose n tasks form a chain a -> b -> c ...,
    all PENDING so the real scheduler drives it. Returns (mission_id, [task ids in order])."""
    created = {"missions": [], "agent": None}

    def _make(n=4, max_attempts=3):
        with get_conn(test_db_url) as conn:
            if created["agent"] is None:
                created["agent"] = repo.create_agent(conn, LEASE_AGENT, capabilities=[]).id
            mission = repo.create_mission(conn, "chaos test")
            ids = []
            for i in range(n):
                task = repo.create_task(
                    conn, mission.id, f"t{i}", created["agent"], f"step {i}",
                    depends_on=[ids[-1]] if ids else None, max_attempts=max_attempts,
                )
                ids.append(task.id)
            repo.set_mission_status(conn, mission.id, MissionStatus.RUNNING)
        created["missions"].append(mission.id)
        return mission.id, ids

    yield _make

    with get_conn(test_db_url) as conn:
        for mission_id in created["missions"]:
            conn.execute("DELETE FROM missions WHERE id = %s", (mission_id,))
        if created["agent"] is not None:
            conn.execute("DELETE FROM agents WHERE id = %s", (created["agent"],))


@pytest.fixture
def counting_runner():
    """A runner that takes `delay` seconds and counts how often each step was actually executed."""
    calls: dict[str, int] = {}

    def make(delay=0.4):
        def run(agent, instruction, dependencies, *, tools, provider=None):
            calls[instruction] = calls.get(instruction, 0) + 1
            time.sleep(delay)
            return AgentResult(output=f"{instruction} done")

        return run

    make.calls = calls
    return make


def proxied_queues(proxy: ChaosProxy, prefix: str, socket_timeout: float = 1.0):
    """Queues whose Redis traffic goes through `proxy`."""
    from core.queue import make_redis

    client = make_redis(f"redis://127.0.0.1:{proxy.port}/0", socket_timeout=socket_timeout)
    return build_queues(client, prefix=prefix)


def fresh_prefix() -> str:
    return f"agentos-chaos-{uuid4().hex[:8]}"


@pytest.fixture
def redis_cleanup(redis_client):
    prefixes = []
    yield prefixes
    for prefix in prefixes:
        redis_client.delete(f"{prefix}:tasks", f"{prefix}:events")


__all__ = ["chain", "counting_runner", "db_proxy", "redis", "redis_proxy"]
