import os
from uuid import uuid4

import psycopg
import pytest
import redis
from psycopg.rows import dict_row

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