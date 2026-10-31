import psycopg
import pytest
from psycopg.rows import dict_row

from core.config import settings
from scripts.migrate import run as run_migrations

TEST_DB = "agentos_test"


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
        pytest.skip("Postgres not available (run `make up`)")

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