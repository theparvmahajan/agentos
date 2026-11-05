import os
import queue
import subprocess
import threading
from uuid import uuid4

import psycopg
import pytest
import redis
from psycopg.rows import dict_row
from starlette.websockets import WebSocketDisconnect

from core.config import settings
from core.queue import build_queues
from scripts.migrate import run as run_migrations

TEST_DB = "agentos_test"


def unavailable(message: str) -> None:
    """On a laptop a missing service skips the tests that need it. In CI that would let a
    broken setup pass silently, so there it is a failure."""
    if os.environ.get("CI"):
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture(scope="session")
def test_db_url() -> str:
    base = settings.database_url.rpartition("/")[0]
    try:
        with psycopg.connect(settings.database_url, autocommit=True) as admin:
            exists = admin.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)
            ).fetchone()
            if not exists:
                admin.execute(f"CREATE DATABASE {TEST_DB}")
    except psycopg.OperationalError:
        unavailable("Postgres not available (run `make up`)")

    url = f"{base}/{TEST_DB}"
    run_migrations(url)
    return url


@pytest.fixture
def conn(test_db_url):
    """Each test runs inside a transaction that is rolled back afterwards."""
    c = psycopg.connect(test_db_url, row_factory=dict_row)
    try:
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture(scope="session")
def redis_client():
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        client.ping()
    except redis.RedisError:
        unavailable("Redis not available (run `make up`)")
    return client


@pytest.fixture
def queues(redis_client):
    """Fresh streams under a unique prefix, so tests never touch real data or each other."""
    q = build_queues(redis_client, prefix=f"agentos-test-{uuid4().hex[:8]}")
    q.ensure_groups()
    yield q
    redis_client.delete(q.tasks.name, q.events.name)


@pytest.fixture(scope="session")
def docker_ready() -> None:
    """The sandbox image must be present locally (`make sandbox`). Skips on a laptop, fails in CI."""
    try:
        found = subprocess.run(
            ["docker", "image", "inspect", settings.code_exec_image], capture_output=True, timeout=30, check=False
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        found = False
    if not found:
        unavailable(f"Docker image {settings.code_exec_image} not available (run `make sandbox`)")


class Feed:
    """Reads a TestClient WebSocket on one background thread. Tests then wait with a timeout
    instead of blocking forever, and no message is ever lost to a second reader."""

    def __init__(self, ws):
        self._items: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, args=(ws,), daemon=True).start()

    def _read(self, ws) -> None:
        try:
            while True:
                self._items.put(ws.receive_json())
        except Exception as e:  # noqa: BLE001 - WebSocketDisconnect when the server closes
            self._items.put(e)

    def next(self, timeout: float = 5.0) -> dict:
        try:
            item = self._items.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(f"no message within {timeout}s") from None
        if isinstance(item, Exception):
            raise item
        return item

    def close_code(self, timeout: float = 5.0) -> int:
        """The code the server closed with (skips nothing: the next item must be the close)."""
        try:
            item = self._items.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(f"the server did not close the stream within {timeout}s") from None
        assert isinstance(item, WebSocketDisconnect), f"expected a close, got {item!r}"
        return item.code

    def until_closed(self, timeout: float = 20.0) -> tuple[list[dict], int]:
        """Every message up to the server's close, and the close code."""
        messages: list[dict] = []
        while True:
            try:
                item = self._items.get(timeout=timeout)
            except queue.Empty:
                raise AssertionError(f"stream still open after {timeout}s") from None
            if isinstance(item, WebSocketDisconnect):
                return messages, item.code
            if isinstance(item, Exception):
                raise item
            messages.append(item)

    def assert_silent(self, seconds: float = 0.6) -> None:
        try:
            item = self._items.get(timeout=seconds)
        except queue.Empty:
            return
        raise AssertionError(f"expected silence, got {item!r}")


@pytest.fixture
def make_feed():
    return Feed
