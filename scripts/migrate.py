"""Apply the SQL files in infrastructure/db/migrations that have not been applied yet.

    python -m scripts.migrate        (or: make migrate)

If the database is not running (usually: `make up` was not run, or Docker is stopped) it says so
within CONNECT_TIMEOUT_SECONDS instead of waiting.

Changing a table needs a lock on it. If another database session is holding that table (a running
API, scheduler or worker, or a database tool with an open transaction), Postgres makes the
migration WAIT, silently and for as long as it takes. So the migration gives up after
LOCK_TIMEOUT_SECONDS and says which sessions are in the way.
"""

import pathlib
import sys

import psycopg
from psycopg import errors

from core.config import settings

MIGRATIONS_DIR = pathlib.Path("infrastructure/db/migrations")
LOCK_TIMEOUT_SECONDS = 10
CONNECT_TIMEOUT_SECONDS = 5


class MigrationError(RuntimeError):
    """The migration could not run. The message says why and what to do."""


class MigrationBlocked(MigrationError):
    """Another database session holds a lock this migration needs."""


class DatabaseUnreachable(MigrationError):
    """Could not connect: the database is not running or not reachable."""


def _connect(database_url: str, *, autocommit: bool = False) -> psycopg.Connection:
    try:
        return psycopg.connect(
            database_url, autocommit=autocommit, connect_timeout=CONNECT_TIMEOUT_SECONDS
        )
    except psycopg.OperationalError as e:
        reason = str(e).strip().splitlines()[0]
        raise DatabaseUnreachable(
            f"cannot connect to the database ({reason}).\n"
            "Is it running? Start it with:  make up\n"
            "and check it with:             docker compose ps   (postgres should be Up)\n"
            "On Windows, Docker Desktop itself must be running first."
        ) from e


def _other_sessions(database_url: str) -> str:
    """One line per other open session in this database, to show who is in the way."""
    try:
        with _connect(database_url, autocommit=True) as conn:
            rows = conn.execute(
                """
                SELECT pid, state, application_name,
                       coalesce(extract(epoch FROM now() - xact_start)::int, 0) AS open_seconds,
                       left(regexp_replace(query, '\\s+', ' ', 'g'), 70) AS query
                FROM pg_stat_activity
                WHERE datname = current_database() AND pid <> pg_backend_pid()
                  AND backend_type = 'client backend'
                ORDER BY xact_start NULLS LAST
                """
            ).fetchall()
    except (psycopg.Error, MigrationError):
        return "  (could not list the other sessions)"
    if not rows:
        return "  (no other sessions found)"
    return "\n".join(
        f"  pid {pid}  {state or '?':<22} app={app or '-':<20} transaction open {secs}s  {query!r}"
        for pid, state, app, secs, query in rows
    )


def run(database_url: str, *, lock_timeout_seconds: float | None = None) -> None:
    if lock_timeout_seconds is None:
        lock_timeout_seconds = LOCK_TIMEOUT_SECONDS  # read now, so it can be changed
    try:
        with _connect(database_url) as conn:
            conn.execute(f"SET lock_timeout = '{int(lock_timeout_seconds * 1000)}ms'")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    filename   TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            applied = {row[0] for row in conn.execute("SELECT filename FROM schema_migrations")}

            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in applied:
                    continue
                print(f"applying {path.name}", flush=True)
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))

            conn.commit()
    except errors.LockNotAvailable as e:
        raise MigrationBlocked(
            f"could not get a lock within {lock_timeout_seconds:g}s: another session is holding a "
            "table this migration changes.\nStop the API, scheduler and workers, close any "
            "database tool (psql, DBeaver...) and run it again. Sessions in this database:\n"
            + _other_sessions(database_url)
        ) from e
    print("migrations up to date", flush=True)


def main() -> None:
    try:
        run(settings.database_url)
    except MigrationError as e:
        sys.exit(f"migration stopped: {e}")


if __name__ == "__main__":
    main()
