from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from core.config import settings


@contextmanager
def get_conn(database_url: str | None = None) -> Iterator[psycopg.Connection]:
    # connect_timeout: a database that does not answer must fail the call, not hang it.
    with psycopg.connect(
        database_url or settings.database_url,
        row_factory=dict_row,
        connect_timeout=settings.db_connect_timeout_seconds,
        # TCP keepalives and tcp_user_timeout (Linux; other systems ignore them): a connection whose
        # network vanished is closed after about db_network_timeout_seconds instead of hanging.
        # NOT covered: a middlebox that acknowledges data but never answers (see docs/RELIABILITY.md).
        keepalives=1,
        keepalives_idle=int(settings.db_network_timeout_seconds),
        keepalives_interval=10,
        keepalives_count=3,
        tcp_user_timeout=int(settings.db_network_timeout_seconds * 1000),
    ) as conn:
        yield conn