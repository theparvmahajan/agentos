"""The migration runner and migration 002, each on its own throwaway database."""

import shutil
import threading
import time
import uuid
from pathlib import Path

import psycopg
import pytest

from core.config import settings
from scripts import migrate

SQL_002 = Path("infrastructure/db/migrations/002_change_notifications.sql")
TRIGGERS = ["missions_notify", "tasks_notify"]


@pytest.fixture
def scratch(test_db_url):
    """Create empty databases next to the test database; drop them all afterwards."""
    base, created = test_db_url.rpartition("/")[0], []

    def make() -> str:
        name = f"agentos_mig_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(test_db_url, autocommit=True) as admin:
            admin.execute(f"CREATE DATABASE {name}")
        created.append(name)
        return f"{base}/{name}"

    yield make
    with psycopg.connect(test_db_url, autocommit=True) as admin:
        for name in created:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def only_001(tmp_path, monkeypatch):
    """Make the runner see only migration 001: the state before 002 existed."""
    shutil.copy("infrastructure/db/migrations/001_init.sql", tmp_path / "001_init.sql")
    real = migrate.MIGRATIONS_DIR

    def apply(url):
        monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)
        migrate.run(url)
        monkeypatch.setattr(migrate, "MIGRATIONS_DIR", real)

    return apply


def triggers(url) -> list[str]:
    with psycopg.connect(url) as c:
        rows = c.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal").fetchall()
    return sorted(r[0] for r in rows)


def recorded(url) -> list[str]:
    with psycopg.connect(url) as c:
        return [r[0] for r in c.execute("SELECT filename FROM schema_migrations ORDER BY 1")]


def in_background(fn):
    out = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - reported to the test thread
            out["error"] = e

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, out


def test_a_fresh_database_gets_every_migration_recorded_and_both_triggers(scratch):
    url = scratch()
    migrate.run(url)
    assert recorded(url) == sorted(p.name for p in Path("infrastructure/db/migrations").glob("*.sql"))
    assert triggers(url) == TRIGGERS


def test_running_migrations_again_applies_nothing(scratch, capsys):
    url = scratch()
    migrate.run(url)
    capsys.readouterr()
    migrate.run(url)
    out = capsys.readouterr().out
    assert "applying" not in out and "up to date" in out


def test_applying_002_by_hand_first_does_not_break_make_migrate(scratch, only_001):
    """What happens if the file is pasted into psql before `make migrate`: it must still work,
    must not duplicate the triggers, and must record the migration."""
    url = scratch()
    only_001(url)
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(SQL_002.read_text())  # by hand
        c.execute(SQL_002.read_text())  # and pasted a second time
    assert recorded(url) == ["001_init.sql"]

    migrate.run(url)  # no error

    assert recorded(url) == ["001_init.sql", "002_change_notifications.sql"]
    assert triggers(url) == TRIGGERS  # exactly one of each


def test_a_session_writing_to_a_table_makes_the_migration_fail_fast_and_name_the_culprit(
    scratch, only_001
):
    url = scratch()
    only_001(url)
    blocker = psycopg.connect(url, application_name="blocker-session")
    blocker.execute("LOCK TABLE tasks IN ROW EXCLUSIVE MODE")  # as a writer's open transaction would
    try:
        started = time.time()
        thread, out = in_background(lambda: migrate.run(url, lock_timeout_seconds=1))
        thread.join(15)
        waited = time.time() - started
        assert not thread.is_alive(), "the migration hung instead of giving up"
    finally:
        blocker.rollback()
        blocker.close()

    error = out["error"]
    assert isinstance(error, migrate.MigrationBlocked) and waited < 8
    assert "could not get a lock within 1s" in str(error)
    assert "blocker-session" in str(error) and "LOCK TABLE tasks" in str(error)
    assert recorded(url) == ["001_init.sql"]  # nothing half-applied

    migrate.run(url)  # and once the blocker is gone it simply works
    assert recorded(url) == ["001_init.sql", "002_change_notifications.sql"]


def test_a_session_that_only_reads_does_not_block_the_migration(scratch, only_001):
    url = scratch()
    only_001(url)
    reader = psycopg.connect(url)
    reader.execute("SELECT count(*) FROM tasks")  # a transaction left open after a plain SELECT
    try:
        thread, out = in_background(lambda: migrate.run(url, lock_timeout_seconds=3))
        thread.join(15)
    finally:
        reader.rollback()
        reader.close()
    assert not thread.is_alive() and "error" not in out
    assert triggers(url) == TRIGGERS


def test_the_command_line_entry_point_exits_with_a_readable_message_when_blocked(
    scratch, only_001, monkeypatch
):
    url = scratch()
    only_001(url)
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(migrate, "LOCK_TIMEOUT_SECONDS", 1)
    blocker = psycopg.connect(url)
    blocker.execute("LOCK TABLE missions IN ROW EXCLUSIVE MODE")
    try:
        with pytest.raises(SystemExit) as exit_info:
            migrate.main()
    finally:
        blocker.rollback()
        blocker.close()
    assert str(exit_info.value).startswith("migration stopped: could not get a lock within 1s")


def test_reapplying_002_over_existing_triggers_does_not_wait_for_readers(scratch):
    """The hand-applied case again, but with something holding the table open after a SELECT.
    Dropping and recreating a trigger would need a lock that waits for that reader; checking
    first and only creating what is missing does not."""
    url = scratch()
    migrate.run(url)  # triggers exist
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("DELETE FROM schema_migrations WHERE filename = '002_change_notifications.sql'")
    reader = psycopg.connect(url)
    reader.execute("SELECT count(*) FROM tasks")
    try:
        thread, out = in_background(lambda: migrate.run(url, lock_timeout_seconds=2))
        thread.join(15)
    finally:
        reader.rollback()
        reader.close()
    assert not thread.is_alive() and "error" not in out
    assert recorded(url)[-1] == "002_change_notifications.sql" and triggers(url) == TRIGGERS


# ---------- the database is not running ----------

def test_a_database_that_is_not_running_gives_advice_instead_of_a_traceback():
    with pytest.raises(migrate.DatabaseUnreachable) as exc:
        migrate.run("postgresql://agentos:agentos@127.0.0.1:1/agentos")  # nothing listens on port 1
    message = str(exc.value)
    assert "cannot connect to the database" in message
    assert "make up" in message and "docker compose ps" in message and "Docker Desktop" in message


def test_an_address_that_never_answers_is_given_up_on_quickly(monkeypatch):
    monkeypatch.setattr(migrate, "CONNECT_TIMEOUT_SECONDS", 1)
    started = time.time()
    with pytest.raises(migrate.DatabaseUnreachable):
        migrate.run("postgresql://agentos:agentos@192.0.2.1:5432/agentos")  # reserved: no host
    assert time.time() - started < 10


def test_the_command_line_entry_point_exits_with_the_same_advice(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql://agentos:agentos@127.0.0.1:1/agentos")
    with pytest.raises(SystemExit) as exit_info:
        migrate.main()
    assert str(exit_info.value).startswith("migration stopped: cannot connect to the database")
    assert "make up" in str(exit_info.value)
